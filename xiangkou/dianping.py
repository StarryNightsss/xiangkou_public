"""点评业务只读 API 的适配层。"""

from __future__ import annotations

from typing import Any

import httpx


class DianpingError(RuntimeError):
    pass


class DianpingUnauthorized(DianpingError):
    pass


class DianpingRejected(DianpingError):
    pass


class DianpingClient:
    def __init__(self, base_url: str, timeout: float = 3.0, transport: httpx.AsyncBaseTransport | None = None):
        # 点评是本地业务服务；忽略宿主机代理变量，避免 localhost 请求误走代理。
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport, trust_env=False)

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, path: str, params: dict[str, Any] | None = None,
                       headers: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.get(path, params=params, headers=headers)
            if response.status_code == 401:
                raise DianpingUnauthorized("点评登录已失效")
            response.raise_for_status()
            body = response.json()
        except DianpingUnauthorized:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise DianpingError("点评服务暂时不可用") from exc
        if not isinstance(body, dict) or body.get("success") is not True:
            raise DianpingRejected(str(body.get("errorMsg", "点评接口返回异常")) if isinstance(body, dict) else "点评接口返回异常")
        return body

    async def _post(self, path: str, *, form: dict[str, str] | None = None,
                    payload: dict[str, str] | None = None,
                    headers: dict[str, str] | None = None) -> Any:
        try:
            response = await self._client.post(path, data=form, json=payload, headers=headers)
            if response.status_code == 401:
                raise DianpingUnauthorized("点评登录已失效")
            response.raise_for_status()
            body = response.json()
        except DianpingUnauthorized:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise DianpingError("点评服务暂时不可用") from exc
        if not isinstance(body, dict) or body.get("success") is not True:
            raise DianpingRejected(str(body.get("errorMsg", "点评接口返回异常")) if isinstance(body, dict) else "点评接口返回异常")
        return body.get("data")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return (await self._request(path, params)).get("data")

    async def list_types(self) -> list[dict[str, Any]]:
        return await self._get("/shop-type/list") or []

    async def send_login_code(self, email: str) -> None:
        await self._post("/user/code", form={"email": email})

    async def login(self, email: str, code: str) -> str:
        token = await self._post("/user/login", payload={"email": email, "code": code})
        if not isinstance(token, str) or not token:
            raise DianpingError("点评登录响应缺少令牌")
        return token

    async def guest_login(self) -> str:
        """游客登录（免验证码）；走同一个客户端，避免写死后端地址。"""
        token = await self._post("/user/guest-login")
        if not isinstance(token, str) or not token:
            raise DianpingError("点评游客登录响应缺少令牌")
        return token

    async def current_user(self, token: str) -> dict[str, Any]:
        user = (await self._request("/user/me", headers={"authorization": token})).get("data")
        if not isinstance(user, dict) or not isinstance(user.get("id"), int):
            raise DianpingUnauthorized("点评登录已失效")
        return {"id": user["id"], "nickName": user.get("nickName"), "icon": user.get("icon")}

    async def logout(self, token: str) -> None:
        await self._post("/user/logout", headers={"authorization": token})

    async def list_areas(self) -> list[str]:
        return await self._get("/shop/areas") or []

    async def get_shop(self, shop_id: int) -> dict[str, Any]:
        return await self._get(f"/shop/{shop_id}") or {}

    async def get_shops(self, ids: list[int]) -> list[dict[str, Any]]:
        return await self._get("/shop/batch", {"ids": ",".join(map(str, ids))}) or []

    async def search_shops(
        self, type_id: int, page: int = 1, size: int = 10, keyword: str | None = None,
        area: str | None = None, max_price: int | None = None, min_score: int | None = None,
        sort: str = "score",
        has_voucher: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"typeId": type_id, "page": page, "size": size, "sort": sort}
        if keyword:
            params["keyword"] = keyword
        if area:
            params["area"] = area
        if max_price is not None:
            params["maxPrice"] = max_price
        if min_score is not None:
            params["minScore"] = min_score
        if has_voucher:
            params["hasVoucher"] = "true"
        body = await self._request("/shop/search", params)
        return {"items": body.get("data") or [], "total": body.get("total") or 0}

    async def list_vouchers(self, shop_id: int) -> list[dict[str, Any]]:
        return await self._get(f"/voucher/list/{shop_id}") or []

    async def create_voucher_order(self, voucher_id: int, kind: str, token: str) -> str:
        path = (f"/voucher-order/seckill/{voucher_id}" if kind == "seckill"
                else f"/voucher-order/ordinary/{voucher_id}")
        order_id = await self._post(path, headers={"authorization": token})
        if not isinstance(order_id, int) or order_id <= 0:
            raise DianpingError("点评订单响应缺少订单号")
        return str(order_id)

    async def get_voucher_order(self, order_id: str, token: str) -> dict[str, Any]:
        order = (await self._request(f"/voucher-order/{order_id}", headers={"authorization": token})).get("data")
        if not isinstance(order, dict) or str(order.get("id")) != order_id:
            raise DianpingError("点评订单详情格式不正确")
        return {"order_id": order_id, "voucher_id": order.get("voucherId"),
                "status": order.get("status"), "created_at": order.get("createTime"),
                "voucher_type": order.get("voucherType"), "pay_cents": order.get("payValue")}

    async def my_orders(self, token: str, held_only: bool = False, page: int = 1) -> dict[str, Any]:
        path = "/voucher-order/mine/vouchers" if held_only else "/voucher-order/mine"
        body = await self._request(path, {"page": page}, headers={"authorization": token})
        items = body.get("data")
        if not isinstance(items, list):
            raise DianpingError("个人券订单格式不正确")
        return {"items": items, "total": body.get("total", len(items)), "page": page}

    async def cancel_voucher_order(self, order_id: str, token: str) -> None:
        result = await self._post(f"/voucher-order/{order_id}/cancel", headers={"authorization": token})
        if str(result) != order_id:
            raise DianpingError("点评取消订单响应不一致")

    async def find_voucher_order(self, voucher_id: int, token: str) -> dict[str, Any] | None:
        order = (await self._request(f"/voucher-order/of/voucher/{voucher_id}",
                                     headers={"authorization": token})).get("data")
        if order is None:
            return None
        if not isinstance(order, dict) or not isinstance(order.get("id"), int):
            raise DianpingError("点评订单查询格式不正确")
        return {"order_id": str(order["id"]), "voucher_id": order.get("voucherId"),
                "status": order.get("status"), "created_at": order.get("createTime")}

    async def list_blogs(self, shop_id: int) -> list[dict[str, Any]]:
        return await self._get("/blog/of/shop", {"shopId": shop_id, "current": 1}) or []

    async def search_blogs(self, shop_id: int, keyword: str) -> list[dict[str, Any]]:
        return await self._get("/blog/search", {"shopId": shop_id, "keyword": keyword, "current": 1}) or []

    async def pay_mock_order(self, order_id: str, amount: int, token: str) -> None:
        await self._post(f"/voucher-order/{order_id}/pay/mock", form={"expectedPayCents": str(amount)},
                         headers={"authorization": token})
