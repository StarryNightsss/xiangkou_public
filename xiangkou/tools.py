"""Dianping read-tool registry shared by workflows and model declarations."""
from .protocol import ToolSpec
from .tool_args import BlogSearchArgs, SearchShopsArgs, ShopIdArgs, ShopIdsArgs


_DEFINITIONS = {
    'list_types': ('读取店铺分类', None),
    'list_areas': ('读取可用商圈', None),
    'search_shops': ('按类型、区域、预算和排序检索店铺', SearchShopsArgs),
    'get_shop': ('查询候选店铺详情，核对价格、评分和坐标', ShopIdArgs),
    'get_shops': ('批量查询店铺详情', ShopIdsArgs),
    'list_vouchers': ('查询候选店铺当前上架优惠券', ShopIdArgs),
    'list_blogs': ('查询候选店铺的探店笔记来源', ShopIdArgs),
    'search_blogs': ('在候选店铺笔记中按固定关键词检索证据', BlogSearchArgs),
}


def discovery_tools(executors):
    return {name: ToolSpec(name, executor, *_DEFINITIONS.get(name, ('', None)))
            for name, executor in executors.items()}
