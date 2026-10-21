"""模型和工作流共用的点评工具参数契约。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SearchShopsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type_id: int = Field(gt=0)
    page: int = Field(default=1, ge=1, le=100000)
    size: int = Field(default=10, ge=1, le=20)
    keyword: Optional[str] = Field(default=None, max_length=80)
    area: Optional[str] = Field(default=None, max_length=80)
    max_price: Optional[int] = Field(default=None, ge=0, le=1000000)
    min_score: Optional[int] = Field(default=None, ge=0, le=50)
    sort: Literal["score", "price"] = "score"
    has_voucher: bool = False

class ShopIdArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shop_id: int = Field(gt=0)


class ShopIdsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[int] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def ids_are_positive(self):
        if any(shop_id <= 0 for shop_id in self.ids):
            raise ValueError("店铺 ID 必须是正整数")
        return self


class BlogSearchArgs(ShopIdArgs):
    keyword: str = Field(min_length=2, max_length=40)
