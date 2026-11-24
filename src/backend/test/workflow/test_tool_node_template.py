# ruff: noqa: RUF002, RUF003 - Chinese prose, full-width punctuation is intended
"""F005 T013 — 工具清单即模板目录：``ToolServices.get_tool_list`` 的每个子工具
都自带 ``node_template``。

"前端 getToolTree 搬到后端"还有另一半要守：消费方拿到清单时不必再各自拼装
模板。三条检查：

1. 每个子工具带 ``node_template``，且与 ``get_tool_node_template`` 的产出一致
   ——清单与模板生成器不漂移；
2. 既有字段原样（id/name/tool_key/api_params/…）——加字段不能顺带改字段；
3. 对 ``get_tool_node_template`` 的输入就是子工具本身——数据只有一个来源。

DAO 与 FGA 判定打桩：本文件守的是装配逻辑，DAO 查询语义归 DAO 自己的测试。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from bisheng.common.dependencies.user_deps import UserPayload
from bisheng.tool.domain.models.gpts_tools import GptsTools, GptsToolsType
from bisheng.tool.domain.services.tool import ToolServices
from bisheng.workflow.common.node_templates import get_tool_node_template

TOOL_TYPE_ID = 1

API_PARAMS = [
    {"name": "query", "description": "检索词", "required": True},
    {"name": "limit", "description": None, "required": False},
]


def make_tool(**overrides) -> GptsTools:
    fields = {
        "id": 7,
        "type": TOOL_TYPE_ID,
        "name": "query_api",
        "tool_key": "tool_key_1",
        "desc": "查询接口",
        "api_params": API_PARAMS,
        "is_preset": 0,
    }
    fields.update(overrides)
    return GptsTools(**fields)


async def get_list(monkeypatch, *, action="use", tools=None):
    monkeypatch.setattr(
        "bisheng.tool.domain.services.tool.GptsToolsDao.aget_tenant_tool_type",
        AsyncMock(return_value=[GptsToolsType(id=TOOL_TYPE_ID, name="预设组", is_preset=0)]),
    )
    monkeypatch.setattr(
        "bisheng.tool.domain.services.tool.batch_check_business_actions",
        AsyncMock(return_value={str(TOOL_TYPE_ID): {action}}),
    )
    monkeypatch.setattr(
        "bisheng.tool.domain.services.tool.GptsToolsDao.aget_list_by_type",
        AsyncMock(return_value=tools if tools is not None else [make_tool()]),
    )
    login_user = UserPayload(user_id=12, user_name="owner", user_role=[], tenant_id=1)
    return await ToolServices(login_user=login_user).get_tool_list(None, action)


async def test_each_child_carries_node_template(monkeypatch):
    """清单里每个子工具都有 node_template——不是只有第一个、不是只有部分。"""
    tools = [
        make_tool(id=7, name="query_api", tool_key="tool_key_1"),
        make_tool(id=8, name="submit_api", tool_key="tool_key_2", api_params=[]),
    ]
    res = await get_list(monkeypatch, tools=tools)

    children = res[0].children
    assert [child.id for child in children] == [7, 8]
    assert all(child.node_template is not None for child in children)
    # 没有参数的工具也生成模板（params 空、output 组在），不炸
    assert children[1].node_template["group_params"][0]["params"] == []


async def test_existing_child_fields_unchanged(monkeypatch):
    """加字段不得顺带改既有字段：清单子工具与原行逐字段相等（node_template 除外）。"""
    tool = make_tool()
    res = await get_list(monkeypatch, tools=[tool])

    child = res[0].children[0]
    original = tool.model_dump()
    for key, value in original.items():
        assert getattr(child, key) == value, f"字段 {key} 在装配中被改动"


async def test_node_template_matches_generator(monkeypatch):
    """child.node_template == get_tool_node_template(child)——装配不夹带私货。"""
    res = await get_list(monkeypatch)

    child = res[0].children[0]
    expected = get_tool_node_template(child.model_dump())
    assert child.node_template == expected
    # 形状契约再钉一遍（与 test_workflows 的冻结用例同源）
    assert child.node_template["tool_key"] == "tool_key_1"
    assert child.node_template["type"] == "tool"
    assert [p["key"] for p in child.node_template["group_params"][0]["params"]] == ["query", "limit"]


# ---------------------------------------------------------------------------
# 写路径回归（G8 第一轮 C1）：清单加 node_template 不得改写模型
# ---------------------------------------------------------------------------


def test_write_model_children_are_mapped_rows():
    """请求体解析出的 children 必须是映射行 ``GptsTools``——DAO 直接 ``session.add_all`` 它们。

    曾把 ``GptsToolsTypeRead.children`` 改成非表读模型，v1/v2 建工具全部
    ``UnmappedInstanceError``；端点测试打桩了 ``add_tools`` 所以没抓到。
    """
    from sqlalchemy import inspect

    from bisheng.tool.domain.models.gpts_tools import GptsToolsTypeRead

    req = GptsToolsTypeRead.model_validate({"name": "t", "children": [{"name": "op", "desc": "d"}]})
    child = req.children[0]
    assert type(child) is GptsTools
    assert inspect(child).mapper.class_ is GptsTools


async def test_insert_tool_type_persists_children(monkeypatch, tmp_path):
    """真实 DAO 落库（不打桩）：``insert_tool_type`` 能把请求体里的 children 写进表。"""
    import contextlib

    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel import Session, select
    from sqlmodel.ext.asyncio.session import AsyncSession

    from bisheng.tool.domain.models.gpts_tools import GptsToolsDao, GptsToolsTypeRead

    db_file = tmp_path / "tools.db"
    engine = create_engine(f"sqlite:///{db_file}")
    GptsTools.metadata.create_all(engine, tables=[GptsToolsType.__table__, GptsTools.__table__])
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")

    @contextlib.asynccontextmanager
    async def fake_async():
        async with AsyncSession(async_engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr("bisheng.tool.domain.models.gpts_tools.get_async_db_session", fake_async)

    req = GptsToolsTypeRead.model_validate({"name": "t", "children": [{"name": "op", "desc": "d"}]})
    try:
        res = await GptsToolsDao.insert_tool_type(req)
        with Session(engine) as session:
            rows = session.exec(select(GptsTools)).all()
        assert [row.name for row in rows] == ["op"]
        assert rows[0].type == res.id
        assert rows[0].tool_key
    finally:
        await async_engine.dispose()
        engine.dispose()
