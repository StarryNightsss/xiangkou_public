"""Vue 前端的受限业务适配接口；不向浏览器暴露点评令牌。"""
from __future__ import annotations

import base64
import os
from datetime import datetime
import re
from uuid import uuid4
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from .actions import ActionError
from .dianping import DianpingError, DianpingUnauthorized
from .schemas import ChatResponse, ShopCard


class CatalogDraft(BaseModel):
    shop_id: int = Field(gt=0)
    voucher_id: int = Field(gt=0)


class BlogDraft(BaseModel):
    shopId: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=10000)
    images: str = Field(min_length=1, max_length=2000)


class VoucherCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    subTitle: str = Field(default="", max_length=200)
    rules: str = Field(min_length=1, max_length=1000)
    payValue: int = Field(ge=0, le=10_000_000)
    actualValue: int = Field(gt=0, le=10_000_000)
    type: int = Field(ge=0, le=1)
    stock: Optional[int] = Field(default=None, ge=1, le=100000)
    beginTime: Optional[datetime] = None
    endTime: Optional[datetime] = None


def is_catalog_admin(user: dict) -> bool:
    ids = {value.strip() for value in os.getenv("XIANGKOU_CATALOG_ADMIN_IDS", "").split(",") if value.strip()}
    return str(user["id"]) in ids


class ImageUpload(BaseModel):
    data: str = Field(max_length=8_000_000)


def register_web_api(app, require_user, token, check_origin):
    async def call(request, path, method="GET", params=None, payload=None, files=None):
        client = app.state.workflow.client
        try:
            response = await client._client.request(method, path, params=params, json=payload,
                                                   files=files, headers={"authorization": token(request)})
            if response.status_code == 401:
                raise HTTPException(401, "点评登录已失效")
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or body.get("success") is not True:
                raise HTTPException(409, body.get("errorMsg", "点评操作失败") if isinstance(body, dict) else "点评响应异常")
            return body
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(503, "点评服务暂时不可用") from exc

    @app.get("/api/catalog/types")
    async def types(request: Request, user=Depends(require_user)):
        return await call(request, "/shop-type/list")

    @app.get("/api/catalog/shops")
    async def shops(request: Request, keyword: str = Query(default="", max_length=50),
                    typeId: Optional[int] = Query(default=None, gt=0), page: int = Query(default=1, ge=1, le=10000),
                    sort: str = Query(default="score", pattern="^(score|price)$"), user=Depends(require_user)):
        return await call(request, "/shop/catalog", params={"keyword": keyword, "typeId": typeId,
                                                          "page": page, "size": 12, "sort": sort})

    @app.get("/api/catalog/shops/{shop_id}")
    async def shop(request: Request, shop_id: int, user=Depends(require_user)):
        if shop_id <= 0:
            raise HTTPException(422, "店铺 ID 无效")
        return {"shop": (await call(request, f"/shop/{shop_id}")).get("data"),
                "vouchers": (await call(request, f"/voucher/list/{shop_id}")).get("data") or [],
                "blogs": (await call(request, "/blog/of/shop", params={"shopId": shop_id})).get("data") or []}

    @app.get("/api/catalog/capabilities")
    async def capabilities(request: Request, user=Depends(require_user)):
        if not is_catalog_admin(user):
            return {"can_create_voucher": False, "mode": "local_demo"}
        result = (await call(request, "/voucher/manage/capabilities")).get("data") or {}
        return {**result, "mode": "local_demo"}

    @app.post("/api/catalog/shops/{shop_id}/vouchers")
    async def create_voucher(shop_id: int, body: VoucherCreate, request: Request, user=Depends(require_user)):
        check_origin(request)
        if not is_catalog_admin(user):
            raise HTTPException(403, "当前账号没有演示商品管理权限")
        if shop_id <= 0 or body.payValue > body.actualValue or not body.title.strip() or not body.rules.strip():
            raise HTTPException(422, "请核对店铺、标题、金额及使用规则")
        if body.type == 1 and (body.stock is None or body.beginTime is None or body.endTime is None
                               or body.beginTime.tzinfo is not None or body.endTime.tzinfo is not None
                               or body.endTime <= body.beginTime):
            raise HTTPException(422, "秒杀券需要库存和有效的本地起止时间")
        shop = (await call(request, f"/shop/{shop_id}")).get("data")
        if not shop:
            raise HTTPException(404, "店铺不存在")
        payload = body.model_dump(mode="json", exclude_none=True)
        payload.update({"shopId": shop_id, "status": 1})
        payload["title"] = "【演示】" + body.title.strip().removeprefix("【演示】")
        if len(payload["title"]) > 100:
            raise HTTPException(422, "标题含演示标记最多100字")
        if body.type == 0:
            for key in ("stock", "beginTime", "endTime"):
                payload.pop(key, None)
        return await call(request, "/voucher/seckill" if body.type == 1 else "/voucher", method="POST", payload=payload)

    @app.delete("/api/catalog/vouchers/{voucher_id}")
    async def delete_voucher(voucher_id: int, request: Request, user=Depends(require_user)):
        check_origin(request)
        return await call(request, f"/voucher/{voucher_id}", method="DELETE")

    @app.post("/api/catalog/prepare")
    async def prepare(body: CatalogDraft, request: Request, user=Depends(require_user)):
        check_origin(request)
        # Catalog selection is independently re-read and recorded as an owned run;
        # confirmation reuses exactly the same amount/expiry/idempotency checks as chat.
        try:
            shop = await app.state.workflow.client.get_shop(body.shop_id)
            if not shop or shop.get("id") != body.shop_id:
                raise HTTPException(404, "店铺不存在")
            run_id = str(uuid4())
            response = ChatResponse(answer="从店铺详情选择商品券", status="ok", mode="rules", run_id=run_id,
                                    shops=[ShopCard(id=body.shop_id, name=shop["name"], url=f"/web/#/shops/{body.shop_id}", reason="用户在店铺详情选择")])
            await app.state.workflow.runs.save(response, user["id"])
            return await app.state.actions.prepare(user["id"], run_id, body.shop_id, body.voucher_id)
        except ActionError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        except DianpingError as exc:
            raise HTTPException(503, "点评券服务不可用") from exc

    @app.post("/api/my/orders/{order_id}/cancel/prepare")
    async def cancel_preview(order_id: str, request: Request, user=Depends(require_user)):
        check_origin(request)
        if not order_id.isdigit() or len(order_id) > 19:
            raise HTTPException(422, "订单 ID 无效")
        try:
            return await app.state.actions.prepare_cancel(user["id"], order_id, token(request))
        except ActionError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        except DianpingUnauthorized as exc:
            raise HTTPException(401, "点评登录已失效") from exc
        except DianpingError as exc:
            raise HTTPException(409, "无法核实订单状态") from exc

    @app.post("/api/my/orders/{order_id}/complete")
    async def complete_order(order_id: str, request: Request, user=Depends(require_user)):
        check_origin(request)
        if not order_id.isdigit() or len(order_id) > 19:
            raise HTTPException(422, "订单 ID 无效")
        return await call(request, f"/voucher-order/{order_id}/complete", method="POST")

    @app.get("/api/community/blogs")
    async def blogs(request: Request, feed: str = Query(default="hot", pattern="^(hot|follow|me)$"),
                    page: int = Query(default=1, ge=1, le=10000), lastId: int = Query(default=9223372036854775807, ge=0),
                    offset: int = Query(default=0, ge=0, le=100000), user=Depends(require_user)):
        path = {"hot": "/blog/hot", "follow": "/blog/of/follow", "me": "/blog/of/me"}[feed]
        return await call(request, path, params={"current": page, "lastId": lastId, "offset": offset})

    @app.get("/api/community/blogs/{blog_id}")
    async def blog(request: Request, blog_id: int, user=Depends(require_user)):
        if blog_id <= 0:
            raise HTTPException(422, "笔记 ID 无效")
        return await call(request, f"/blog/{blog_id}")

    @app.put("/api/community/blogs/{blog_id}/like")
    async def like(request: Request, blog_id: int, user=Depends(require_user)):
        check_origin(request)
        if blog_id <= 0:
            raise HTTPException(422, "笔记 ID 无效")
        return await call(request, f"/blog/like/{blog_id}", method="PUT")

    @app.get("/api/community/blogs/{blog_id}/comments")
    async def list_comments(request: Request, blog_id: int, user=Depends(require_user)):
        if blog_id <= 0:
            raise HTTPException(422, "笔记 ID 无效")
        return await call(request, f"/blog-comments/{blog_id}")

    @app.post("/api/community/blogs/{blog_id}/comments")
    async def add_comment(request: Request, blog_id: int, payload: dict, user=Depends(require_user)):
        check_origin(request)
        if blog_id <= 0:
            raise HTTPException(422, "笔记 ID 无效")
        payload["blogId"] = blog_id
        return await call(request, "/blog-comments", method="POST", payload=payload)

    @app.get("/api/community/authors/{author_id}/follow")
    async def followed(request: Request, author_id: int, user=Depends(require_user)):
        if author_id <= 0:
            raise HTTPException(422, "用户 ID 无效")
        return await call(request, f"/follow/or/not/{author_id}")

    @app.get("/api/community/users/{user_id}")
    async def user_profile(request: Request, user_id: int, user=Depends(require_user)):
        """个人主页聚合：基础资料 + 详情 + 是否已关注 + 第一页笔记。"""
        if user_id <= 0:
            raise HTTPException(422, "用户 ID 无效")
        basic = (await call(request, f"/user/{user_id}")).get("data")
        if not basic:
            raise HTTPException(404, "用户不存在")
        profile = (await call(request, f"/user/info/{user_id}")).get("data") or None
        # 看自己的主页时没有关注关系，跳过这次调用，避免点评侧无意义查询
        is_self = user_id == user["id"]
        followed_state = False if is_self else bool((await call(request, f"/follow/or/not/{user_id}")).get("data"))
        blogs = (await call(request, "/blog/of/user", params={"id": user_id, "current": 1})).get("data") or []
        return {"user": basic, "profile": profile, "followed": followed_state,
                "is_self": is_self, "blogs": blogs}

    @app.get("/api/community/users/{user_id}/blogs")
    async def user_blogs(request: Request, user_id: int, page: int = Query(default=1, ge=1, le=10000),
                         user=Depends(require_user)):
        if user_id <= 0:
            raise HTTPException(422, "用户 ID 无效")
        return await call(request, "/blog/of/user", params={"id": user_id, "current": page})

    @app.post("/api/community/sign")
    async def sign(request: Request, user=Depends(require_user)):
        """当前账号每日签到；重复签到是幂等的。"""
        check_origin(request)
        return await call(request, "/user/sign", method="POST")

    @app.get("/api/community/sign/count")
    async def sign_count(request: Request, user=Depends(require_user)):
        """当前账号本月的连续签到天数。"""
        return await call(request, "/user/sign/count")

    @app.put("/api/community/authors/{author_id}/follow")
    async def follow(request: Request, author_id: int, enabled: bool, user=Depends(require_user)):
        check_origin(request)
        if author_id <= 0 or author_id == user["id"]:
            raise HTTPException(422, "请选择其他用户")
        return await call(request, f"/follow/{author_id}/{str(enabled).lower()}", method="PUT")

    @app.post("/api/community/images")
    async def upload(body: ImageUpload, request: Request, user=Depends(require_user)):
        check_origin(request)
        try:
            data = base64.b64decode(body.data, validate=True)
        except ValueError as exc:
            raise HTTPException(422, "图片编码无效") from exc
        if not data or len(data) > 5_000_000:
            raise HTTPException(422, "图片最大 5MB")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext, mime = "png", "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            ext, mime = "jpg", "image/jpeg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext, mime = "webp", "image/webp"
        else:
            raise HTTPException(422, "只支持 PNG、JPEG、WebP 图片")
        return await call(request, "/upload/blog", method="POST", files={"file": (f"image.{ext}", data, mime)})

    @app.post("/api/community/blogs")
    async def publish(body: BlogDraft, request: Request, user=Depends(require_user)):
        check_origin(request)
        paths = body.images.split(",")
        if len(paths) > 9 or any(not re.fullmatch(r"/blogs/[0-9]+/[0-9]+/[a-f0-9-]+\.(jpg|png|webp)", path) for path in paths):
            raise HTTPException(422, "请选择上传的图片，最多 9 张")
        shop = (await call(request, f"/shop/{body.shopId}")).get("data")
        if not shop:
            raise HTTPException(404, "关联店铺不存在")
        return await call(request, "/blog", method="POST", payload=body.model_dump())
