"""Native completion, document binding, verification and existing restrictions."""

from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import FastMCP

from mcp_server_odoo.access_control import AccessControlError
from mcp_server_odoo.error_handling import NotFoundError, ValidationError
from mcp_server_odoo.freno import Freno, FrenoError
from mcp_server_odoo.tools import OdooToolHandler


@pytest.fixture
def setup():
    app = FastMCP("test")
    conn = Mock(is_authenticated=True, _base_url="https://odoo.example")
    cfg = Mock(freno=None)
    acl = Mock()
    h = OdooToolHandler(app, conn, acl, cfg)
    activity = dict(
        id=12,
        res_model="sale.order",
        res_id=30,
        summary="Follow up",
        user_id=[7, "Ivan"],
        activity_type_id=[4, "Email"],
        active=True,
    )

    def read(model, ids, fields):
        if model == "mail.activity":
            return [activity] if "res_model" in fields else []
        if model == "mail.message":
            return [dict(id=71, model="sale.order", res_id=30)]
        return [dict(id=30)]

    conn.read.side_effect = read
    conn.execute_kw.return_value = 71
    return h, conn, acl, activity, app


async def complete(setup, preview=False, **overrides):
    args = dict(
        activity_id=12,
        model="sale.order",
        record_id=30,
        feedback="Sent <approved>\nDone",
        preview=preview,
    )
    args.update(overrides)
    return await setup[0]._handle_complete_activity_tool(**args)


async def test_preview_and_registration(setup):
    r = await complete(setup, preview=True)
    assert r["preview"] and not r["completed"]
    setup[1].execute_kw.assert_not_called()
    tools = await setup[4].list_tools()
    t = next(t for t in tools if t.name == "complete_activity")
    assert t.inputSchema["properties"]["preview"]["default"] is True
    assert t.annotations.idempotentHint is False
    assert t.outputSchema["properties"]["message_id"]


async def test_completes_with_feedback_and_native_history(setup):
    r = await complete(setup)
    assert r["success"] and r["completed"] and r["message_id"] == 71
    setup[1].execute_kw.assert_called_once_with(
        "mail.activity", "action_feedback", [[12]], {"feedback": "Sent &lt;approved&gt;<br/>Done"}
    )
    setup[1].unlink.assert_not_called()
    setup[1].write.assert_not_called()


async def test_kept_done_archived_activity(setup):
    old = setup[1].read.side_effect
    setup[1].read.side_effect = lambda m, ids, f: (
        [dict(id=12, active=False)]
        if m == "mail.activity" and f == ["id", "active"]
        else old(m, ids, f)
    )
    assert (await complete(setup))["completed"]


@pytest.mark.parametrize(
    "field,value", [("res_id", 99), ("res_model", "crm.lead"), ("active", False)]
)
async def test_wrong_document_or_already_archived(setup, field, value):
    setup[3][field] = value
    with pytest.raises(ValidationError):
        await complete(setup)
    setup[1].execute_kw.assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    [
        dict(activity_id=0),
        dict(activity_id=True),
        dict(record_id=-1),
        dict(feedback="  "),
        dict(model=""),
    ],
)
async def test_invalid_arguments(setup, overrides):
    with pytest.raises(ValidationError):
        await complete(setup, **overrides)
    setup[1].execute_kw.assert_not_called()


async def test_missing_activity_is_not_success(setup):
    setup[1].read.side_effect = lambda *_: []
    with pytest.raises((NotFoundError, ValidationError)):
        await complete(setup)
    setup[1].execute_kw.assert_not_called()


@pytest.mark.parametrize("case", ["still_active", "missing_history", "wrong_history"])
async def test_does_not_claim_success_without_both_effects(setup, case):
    old = setup[1].read.side_effect

    def read(m, ids, f):
        if case == "still_active" and m == "mail.activity" and f == ["id", "active"]:
            return [dict(id=12, active=True)]
        if case == "missing_history" and m == "mail.message":
            return []
        if case == "wrong_history" and m == "mail.message":
            return [dict(id=71, model="sale.order", res_id=31)]
        return old(m, ids, f)

    setup[1].read.side_effect = read
    r = await complete(setup)
    assert not r["success"] and not r["completed"]
    assert "before retrying" in r["message"]


async def test_uncertain_transport_not_retried(setup):
    setup[1].execute_kw.side_effect = RuntimeError("response lost")
    with pytest.raises(ValidationError, match="outcome uncertain"):
        await complete(setup)
    assert setup[1].execute_kw.call_count == 1


async def test_acl_denied_before_mutation(setup):
    setup[2].validate_model_access.side_effect = AccessControlError("Denied")
    with pytest.raises(ValidationError, match="Access denied"):
        await complete(setup)
    setup[1].execute_kw.assert_not_called()


async def test_restricted_profile_remains_blocked(setup):
    h, conn, acl, _, _ = setup
    h.config.freno = "backoffice"
    with pytest.raises(ValidationError, match="restricted"):
        await complete(setup)
    conn.execute_kw.assert_not_called()
    restricted_app = FastMCP("restricted")
    OdooToolHandler(restricted_app, conn, acl, h.config)
    assert "complete_activity" not in [t.name for t in await restricted_app.list_tools()]
    freno = Freno("backoffice", lambda: 54, Mock())
    with pytest.raises(FrenoError):
        freno.comprobar("mail.activity", "action_feedback", [[12]], {"feedback": "Done"})


async def test_tool_call_returns_structured_verified_result(setup):
    _content, structured = await setup[4].call_tool(
        "complete_activity",
        {
            "activity_id": 12,
            "model": "sale.order",
            "record_id": 30,
            "feedback": "Done",
            "preview": False,
        },
    )
    assert structured["completed"] and structured["message_id"] == 71


async def test_verification_connection_failure_is_uncertain(setup):
    old = setup[1].read.side_effect

    def read(m, ids, f):
        if m == "mail.message":
            raise RuntimeError("Lost connection")
        return old(m, ids, f)

    setup[1].read.side_effect = read
    with pytest.raises(ValidationError, match="outcome uncertain"):
        await complete(setup)
    assert setup[1].execute_kw.call_count == 1
