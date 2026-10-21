"""Durable business dialogue; models suggest intents, workflows own all writes."""
import json
import re
from .actions import ActionError
from .harness import AgentHarness, mask_personal_info
from .schemas import ChatResponse, ShopCard
from .run_context import RunContext


def ordinal(text, pattern, default=None):
    match = re.search(pattern, text)
    return {'一': 0, '二': 1, '三': 2, '1': 0, '2': 1, '3': 2}[match.group(1)] if match else default


# 处理券、个人资产和交易追问；未命中时返回 None，交给找店工作流。
class BusinessWorkflow:
    def __init__(self, discovery, actions, run_context=None, parent_span_id=None):
        self.discovery, self.actions = discovery, actions
        self.states = discovery.business_states
        self.prelude_harness = None
        self.run_context, self.parent_span_id = run_context, parent_span_id

    async def _semantic_intent(self, text, state, harness):
        provider = self.discovery.planner.provider
        if not provider.enabled:
            return {'intent': 'other'}
        try:
            completion = await harness.model_call(lambda: provider.complete(
                '仅判断本地生活业务意图，输出JSON {"intent":..., "shop_keyword":...}。intent只可为 choose_coupon、free_coupon、abandon、held_plan、my_orders、my_vouchers、merchant_vouchers、cancel_order、other。'
                '只有明确查询自己持有的商品券/已领券才用my_vouchers，查询订单、刚才购买/确认是否成功、为什么已买却没有券等交易进度用my_orders。查询某店在售券/代金券/商品用merchant_vouchers，shop_keyword提取当前消息明确出现的店名短词，例如海底捞；不能把商户无券与用户无持券混淆。只有明确放弃未提交草稿时用abandon。不要执行操作、不要输出订单或店铺ID。普通找店用other。',
                [{'role': 'user', 'content': json.dumps({'message': mask_personal_info(text),
                                                       'phase': state.get('phase') if state else None}, ensure_ascii=False)}], max_tokens=100),
                timeout=provider.config.timeout)
            payload = json.loads(completion.text.strip().removeprefix('```json').removesuffix('```').strip())
            intent = payload.get('intent')
            if intent not in {'choose_coupon','free_coupon','abandon','held_plan','my_orders','my_vouchers','merchant_vouchers','cancel_order','other'}:
                raise ValueError('Unsupported business intent')
            harness.record('business_intent', intent=intent, mode='model', usage=completion.usage)
            # A model may assist choosing/reading, but abandonment needs explicit wording.
            return payload if intent != 'abandon' else {'intent': 'other'}
        except Exception as exc:
            harness.record('business_intent_fallback', error=type(exc).__name__)
            return {'intent': 'other'}

    async def _merchant_vouchers(self, request, user_id, harness, keyword):
        from .workflow import _to_card, _apply_detail
        session = request.session_id or (await self.discovery.sessions.load(None, user_id)).id
        result = ChatResponse(answer='', status='ok', mode='rules', run_id=harness.run_id, session_id=session)
        if not isinstance(keyword, str) or not keyword.strip() or keyword not in request.message:
            result.status, result.answer = 'needs_clarification', '你想查哪家店的在售券？请补充店名或门店名称。'
            return await self._finish(result, harness, user_id)
        # Search the merchant independently of prior dining budget / relative price / owned assets.
        types = await harness.call('list_types')
        matches = []
        for category in types[:10]:
            page = await harness.call('search_shops', type_id=category['id'], keyword=keyword, size=3)
            matches.extend(shop for shop in page['items'] if keyword.lower() in str(shop.get('name', '')).lower())
            if matches:
                break
        for shop in matches[:3]:
            coupons = await harness.call('list_vouchers', shop_id=shop['id'])
            result.shops.append(_to_card(_apply_detail(shop, shop, '/shop/search'), self.discovery.web_url, coupons))
        if not result.shops:
            result.status, result.answer = 'no_results', f'本次按店名“{keyword}”未查到门店，尚未查询其在售券；可以补充门店名或商圈。'
        else:
            descriptions = []
            for shop in result.shops:
                items = [f"{v.get('title')}（售价 ¥{v['payValue']/100:g}，券面 ¥{v['actualValue']/100:g}）"
                         for v in shop.coupons if isinstance(v.get('payValue'), int) and isinstance(v.get('actualValue'), int)]
                descriptions.append(shop.name + '：' + ('；'.join(items) if items else '本次在售券接口返回空列表'))
            result.answer = '本次查询的是店铺在售商品券，不是你的持券。' + '\n'.join(descriptions)
            result.answer += ' 付费券只创建待支付订单，提交需确认；具体使用条件请核对券规则。'
        harness.record('merchant_vouchers_read', keyword=keyword, shops=len(result.shops), scope='merchant_catalog')
        return await self._finish(result, harness, user_id)

    async def _finish(self, result, harness, user):
        harness.record('run_end', status=result.status)
        result.events, result.trace = list(harness.events), list(harness.trace)
        if any(event['event']=='business_intent' for event in harness.events):
            result.mode = 'model'
        await self.discovery.runs.save(result, user)
        return result

    async def _join(self, result, harness, user):
        harness.record('business_read_end', child_run_id=result.run_id)
        harness.record('run_end', status=result.status)
        result.events = sorted(list(harness.events) + result.events, key=lambda event: event.get('sequence',0))
        result.trace = list(harness.trace) + result.trace
        await self.discovery.runs.save(result, user)
        return result

    async def run(self, request, user_id, token, on_event=None):
        if request.session_id:
            # Shared by the store, even when API creates a workflow per request.
            async with self.states.lock(user_id, request.session_id):
                return await self._run(request, user_id, token, on_event)
        return await self._run(request, user_id, token, on_event)

    async def _run(self, request, user_id, token, on_event=None):
        text = request.message
        # 选券状态按账号与会话读取，保存时带版本，防止旧追问覆盖更新后的选择。
        state, revision = self.states.snapshot(user_id, request.session_id)

        def save_slots(session, record):
            nonlocal revision
            revision = self.states.save(user_id, session, record,
                                        expected_revision=revision if session == request.session_id else 0)

        def clear_slots():
            nonlocal revision
            revision = self.states.clear(user_id, request.session_id, expected_revision=revision)
        personal = bool(re.search(r'我的(?:订单|券|优惠券|商品券|代金券|团购券)|我(?:已经)?有的券|我(?:买过|领过)|我(?:现在|目前|已经)?(?:持有|有).{0,30}券', text))
        progress = bool(state and state.get('action_id') and re.search(r'刚刚|刚才|已经|已', text)
                        and re.search(r'购买|买|下单|确认|提交', text)
                        and re.search(r'吗|么|没|不是|查询|查看|看下|成功|状态|为什么', text))
        # 交易进度追问先读个人订单，不让“确认”“刚才”等字词被旧选券任务截获。
        personal = personal or progress
        avoid = bool(re.search(r'买过.*(?:换|避开|不要)|(?:避开|排除).*买过', text))
        cancel = bool(re.search(r'(?:取消|撤销).{0,12}(?:订单|下单)', text))
        buy = bool(re.search(r'领券|领取|下单|购买.*券|帮我领|领.{0,8}方案.*券', text))
        abandon = bool(state and re.search(r'先别下单|不要下单|先不买|取消草稿|放弃|算了|先别领|不领了', text))
        order_reply = bool(state and state.get('phase') == 'awaiting_order' and re.fullmatch(r'(?:订单(?:号|ID|id)?\s*[:：]?\s*)?\d{1,20}', text.strip()))
        cancel = cancel or order_reply
        choice = bool(state and not personal and re.search(r'第[一二三123](?:张|个)|券(?:ID|id|编号)|免费|不要钱|白嫖|确认|继续', text))
        held_plan = bool(personal and re.search(r'券|领过', text) and re.search(r'规划|安排|先.+再|预算|人均|今晚|明天|今天', text))
        held_followup = bool(state and state.get('phase') == 'held_plan' and not (personal or buy or cancel)
                             and not re.search(r'不用.*券|不要.*券|普通推荐|新计划', text)
                             and not ('券' in text and not personal))
        if not (personal or cancel or buy or avoid or abandon or choice or held_plan or held_followup
                or state or re.search(r'券|订单', text)):
            return None
        harness = AgentHarness({'my_orders': lambda: self.discovery.client.my_orders(token, held_only=False),
                                'my_vouchers': lambda: self.discovery.client.my_orders(token, held_only=True),
                                'get_shops': self.discovery.client.get_shops,
                                'list_vouchers': self.discovery.client.list_vouchers,
                                'list_types': self.discovery.client.list_types,
                                'search_shops': self.discovery.client.search_shops}, max_calls=14, on_event=on_event, context=self.run_context or RunContext(max_calls=20), parent_span_id=self.parent_span_id)
        self.prelude_harness = harness
        harness.record('run_start')
        intent = 'other'
        if not (personal or cancel or buy or avoid or abandon or choice or held_plan or held_followup):
            intent_payload = await self._semantic_intent(text, state, harness)
            intent = intent_payload['intent']
            if intent == 'merchant_vouchers':
                return await self._merchant_vouchers(request, user_id, harness, intent_payload.get('shop_keyword'))
            choice = intent in ('choose_coupon', 'free_coupon') and bool(state)
            buy = intent == 'choose_coupon'
            held_plan = intent == 'held_plan'
            personal = intent in ('my_orders', 'my_vouchers')
            cancel = intent == 'cancel_order'
            if intent == 'free_coupon':
                choice, buy = bool(state), True
                text = '换免费的'
            if not (choice or buy or held_plan or personal or cancel):
                if state:
                    clear_slots()
                return None
        # 聊天确认只取出预览；实际订单提交由独立确认接口授权。
        if state and choice and re.search(r'确认|继续', text) and state.get('action_id'):
            result = ChatResponse(answer='请在确认窗口核对并点击确认，聊天消息不会提交订单。',
                                  status='ok', mode='rules', run_id=harness.run_id, session_id=request.session_id)
            result.action_draft = await self.actions.preview(state['action_id'], user_id)
            return await self._finish(result,harness,user_id)
        # 放弃选券与放弃未提交草稿一起提交；保留找店推荐，不取消已经创建的订单。
        if abandon:
            revision = await self.actions.update_business_state(
                self.states, user_id, request.session_id, {'_deleted': True}, revision,
                old_action_id=state.get('action_id'))
            result = ChatResponse(answer='已放弃未提交的业务任务，原推荐条件保留。',status='ok',mode='rules',
                                  run_id=harness.run_id,session_id=request.session_id,business_state='abandoned')
            return await self._finish(result,harness,user_id)
        # 仅排除最近一页已支付或已核销门店，不把待支付或取消记录当作已买过。
        if avoid:
            orders = await harness.call('my_orders')
            ids = [item['shop_id'] for item in orders['items'] if item.get('shop_id') and item.get('status') in (2,3)]
            result = await self.discovery.run(request,user_id,on_event=on_event,extra_excluded=ids,
                                              run_context=harness.context,parent_span_id=harness.run_id)
            result.answer += ' 已排除最近一页已支付或已核销订单对应的店铺。'
            return await self._join(result,harness,user_id)
        # 持券规划每次重读当前资产；优惠未结构化核验，所以费用不直接减券。
        if held_plan or held_followup:
            page = await harness.call('my_vouchers')
            ids = list(dict.fromkeys(item['shop_id'] for item in page['items'] if item.get('shop_id')))
            if ids:
                result = await self.discovery.run(request,user_id,on_event=on_event,required_shops=ids,
                                                  run_context=harness.context,parent_span_id=harness.run_id)
                result.personal_orders = page['items']
                result.business_state = 'held_plan'
                result.answer += ' 方案至少包含一家持有券关联店铺；按原人均价估算，未扣券。使用时段、商品门槛和叠加条件待核对。仅读取最近20条待核销券订单。'
                save_slots(result.session_id,{'phase':'held_plan'})
                return await self._join(result,harness,user_id)
            result = ChatResponse(answer='当前未查到待核销券，不能生成持券方案。可以先查询普通推荐。',status='no_results',mode='rules',
                                  run_id=harness.run_id,session_id=request.session_id)
            clear_slots()
            return await self._finish(result,harness,user_id)
        result = ChatResponse(answer='',status='ok',mode='rules',run_id=harness.run_id,session_id=request.session_id)
        if personal and not cancel and not buy and not choice:
            held = bool(re.search(r'我的(?:券|优惠券|商品券|代金券|团购券)|有的券|领过|我(?:现在|目前|已经)?(?:持有|有).{0,30}券',text)) or intent == 'my_vouchers'
            page = await harness.call('my_vouchers' if held else 'my_orders')
            result.personal_orders = page['items']
            result.answer = f"查到 {page['total']} 条{'已领取或已支付、待核销的券' if held else '订单'}记录，展示最近一页。"
            if held:
                result.answer += ' 持券状态不保证当前可使用，仍需核对券规则与商家。'
                # 券包为空不代表没下单；再读最近订单解释待支付记录，但不自动支付。
                if not page['items']:
                    orders = await harness.call('my_orders')
                    pending = [item for item in orders['items'] if item.get('status') == 1]
                    if pending:
                        result.personal_orders = pending
                        result.answer += f' 另查到最近一页有 {len(pending)} 条待支付订单，列在下方。订单已创建，但未支付，尚未进入待核销券包；不能说尚未下单。'
                    else:
                        result.answer += ' 最近一页也未查到待支付订单；这不代表所有历史订单均不存在。'
            else:
                result.answer += ' 状态1=待支付、2=待核销、3=已核销、4=已取消。待支付说明已创建订单，并不等于没下单；可在订单卡片点击模拟支付并确认；不扣真实资金。'

            if held and re.search(r'去哪|推荐|店|用',text):
                from .workflow import _to_card, _apply_detail
                ids = list(dict.fromkeys(item['shop_id'] for item in page['items'] if item.get('shop_id')))
                shops = await harness.call('get_shops',ids=ids[:20]) if ids else []
                result.shops = [_to_card(_apply_detail(shop,shop,'/shop/batch'),self.discovery.web_url,[]) for shop in shops]
            return await self._finish(result,harness,user_id)
        # 继续选券使用保存的推荐来源；个人资产查询已在前面处理，不沿用旧推荐筛选条件。
        context = state.get('context_run_id') if state and choice else request.context_run_id
        record = await self.discovery.runs.get(context,user_id) if context else (
            await self.discovery.runs.latest_for_session(request.session_id,user_id) if request.session_id else None)
        session = request.session_id or (record.get('session_id') if record else None)
        if not session:
            session = (await self.discovery.sessions.load(None,user_id)).id
        result.session_id = session
        if cancel:
            explicit = re.search(r'订单(?:号|ID|id)?\s*[:：]?\s*(\d{1,20})',text)
            orders = (await harness.call('my_orders'))['items']
            pending = [item for item in orders if item.get('status') == 1 and item.get('voucher_type') == 0]
            order_id = text.strip() if order_reply and text.strip().isdigit() else explicit.group(1) if explicit else pending[0]['order_id'] if pending and re.search(r'刚才|最近|上一',text) else None
            if not order_id:
                result.status,result.answer = 'needs_clarification','请提供要取消的订单号，或说“取消最近的待支付订单”。'
                result.personal_orders = orders
                save_slots(session,{'phase':'awaiting_order'})
                result.business_state = 'awaiting_order'
            else:
                result.action_draft = await self.actions.prepare_cancel(user_id,str(order_id),token)
                save_slots(session,{'phase':'awaiting_confirmation','action_id':result.action_draft['action_id']})
                result.answer = '已生成取消订单草稿，请核对订单号并点击确认。'
            return await self._finish(result,harness,user_id)
        if not record:
            result.status,result.answer = 'needs_clarification','请先生成推荐方案，再指定方案、店铺和优惠券。'
            return await self._finish(result,harness,user_id)
        if record.get('response',{}).get('recommendation_run_id'):
            record = await self.discovery.runs.get(record['response']['recommendation_run_id'],user_id)
            if not record:
                raise ActionError('原推荐已过期，请重新生成方案',409)
        result.recommendation_run_id = record['run_id']
        response = record.get('response',{})
        plans = response.get('itineraries') or []
        plan_index = ordinal(text,r'第([一二三123])(?:个)?方案',state.get('plan_index',0) if choice else 0)
        if plans and plan_index >= len(plans):
            raise ActionError('指定方案不存在',404)
        shops = plans[plan_index]['stops'] if plans else response.get('shops',[])
        stop_index = ordinal(text,r'第([一二三123])(?:家|站|个店)',state.get('stop_index') if choice else 0 if len(shops)==1 else None)
        if stop_index is not None and stop_index >= len(shops):
            raise ActionError('指定店铺不存在',404)
        free = bool(re.search(r'免费|不要钱|白嫖',text)) or bool(choice and state.get('free_only'))
        eligible = []
        for shop in ([shops[stop_index]] if stop_index is not None else shops):
            coupons = await harness.call('list_vouchers',shop_id=shop['id'])
            for coupon in coupons:
                if free and coupon.get('payValue') != 0:
                    continue
                try:
                    self.actions._voucher([coupon],coupon['id'])
                except (ActionError,KeyError):
                    continue
                eligible.append((shop,coupon))
        # Preserve numbering across followups; never silently select another coupon after removal.
        picking = bool(re.search(r'第[一二三123](?:张|个)(?!方案)',text))
        # 按原编号绑定的店铺/券 ID 查找；券消失时保留空位，不能让后续商品顶替“第二张”。
        if choice and picking and state.get('choices'):
            mapping = {(shop['id'],coupon['id']):(shop,coupon) for shop,coupon in eligible}
            eligible = [mapping.get(tuple(pair)) for pair in state['choices']]
        chosen_index = ordinal(text,r'第([一二三123])(?:张(?:券)?|个)(?!方案)',0 if len(eligible)==1 else None)
        voucher_id = re.search(r'券(?:ID|id|编号)\s*[:：]?\s*(\d+)',text)
        selected = next((item for item in eligible if item and item[1].get('id')==int(voucher_id.group(1))),None) if voucher_id else (
            eligible[chosen_index] if chosen_index is not None and chosen_index<len(eligible) else None)
        old_action_id = state.get('action_id') if choice else None
        slots = {'context_run_id':record['run_id'],'plan_index':plan_index,'stop_index':stop_index,'free_only':free}
        if selected:
            shop,coupon = selected
            # 新草稿、选择版本和旧草稿失效在同一事务里更新，避免换券只完成一半。
            def commit_draft(action):
                nonlocal revision
                slots.update(phase='awaiting_confirmation', action_id=action['id'])
                revision = self.actions.commit_business_state(
                    self.states, user_id, session, slots,
                    revision if session == request.session_id else 0,
                    old_action_id=old_action_id, new_action=action)

            result.action_draft = await self.actions.prepare(
                user_id,record['run_id'],shop['id'],coupon['id'],on_prepared=commit_draft)
            result.answer = '已生成券操作草稿，请核对价格、规则并点击确认。'
            slots.update(phase='awaiting_confirmation',action_id=result.action_draft['action_id'])
        else:
            result.status = 'needs_clarification'
            result.answer = '请说“第一张/第二张”或指定券编号，也可以说“换免费的”。' if any(eligible) else '当前没有符合条件的可领取券。'
            if picking:
                result.answer = '所选券已变化或不存在，请按下面最新列表重新选择。'
            eligible = [item for item in eligible if item]
            result.business_choices = [{'index':i+1,'shop_id':shop['id'],'shop_name':shop['name'],'voucher_id':coupon['id'],
                                        'title':coupon.get('title'),'pay_cents':coupon['payValue']} for i,(shop,coupon) in enumerate(eligible)]
            result.shops = [ShopCard.model_validate({**shop,'coupons':[coupon for candidate,coupon in eligible if candidate['id']==shop['id']]}) for shop in shops]
            slots.update(phase='awaiting_coupon',choices=[[shop['id'],coupon['id']] for shop,coupon in eligible])
            revision = await self.actions.update_business_state(
                self.states, user_id, session, slots, revision if session == request.session_id else 0,
                old_action_id=old_action_id)
        result.business_state = slots['phase']
        harness.record('business_state',phase=slots['phase'])
        return await self._finish(result,harness,user_id)
