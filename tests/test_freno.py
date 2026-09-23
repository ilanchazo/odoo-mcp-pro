"""Freno de Nómada (perfil backoffice) contra un Odoo simulado en memoria."""

import pytest

from mcp_server_odoo.freno import Freno, FrenoError

ALBA = 54

PARTNERS = {
    100: {"commercial_partner_id": [100, "Cliente normal"], "category_id": []},
    101: {"commercial_partner_id": [100, "Cliente normal"], "category_id": []},  # contacto hijo
    200: {"commercial_partner_id": [200, "Cecotec"], "category_id": [90]},  # reservada
    201: {"commercial_partner_id": [200, "Cecotec"], "category_id": []},  # hijo de reservada
    300: {"commercial_partner_id": [300, "Margen OK SL"], "category_id": [88]},
    789: {"commercial_partner_id": [789, "AEOL"], "category_id": []},
}
PEDIDOS = {
    1: {"name": "S1", "state": "draft", "user_id": [ALBA, "Alba"], "partner_id": [100, "x"], "order_line": [11]},
    2: {"name": "S2", "state": "sent", "user_id": [ALBA, "Alba"], "partner_id": [100, "x"], "order_line": []},
    3: {"name": "S3", "state": "draft", "user_id": [7, "Iván"], "partner_id": [100, "x"], "order_line": []},
}
LINEAS = {
    11: {"product_id": [500, "Taza"], "name": "Taza", "product_uom_qty": 100, "price_unit": 3.0,
         "purchase_price": 2.0, "discount": 0, "display_type": False, "sequence": 10},
}
EMAILS = {"ya@existe.com": {"id": 101, "name": "Ya", "display_name": "Ya existe", "commercial_partner_id": [100, "x"]},
          "cecotec@cecotec.es": {"id": 201, "name": "C", "display_name": "C", "commercial_partner_id": [200, "Cecotec"]}}


def rpc(model, method, args, kwargs):
    if model == "res.partner" and method == "read":
        return [dict(PARTNERS[i], id=i) for i in args[0] if i in PARTNERS]
    if model == "res.partner" and method == "search_read":
        campo, _, valor = args[0][0]
        if campo == "email" and valor in EMAILS:
            return [EMAILS[valor]]
        return []
    if model == "sale.order" and method == "read":
        return [dict(PEDIDOS[i], id=i) for i in args[0] if i in PEDIDOS]
    if model == "sale.order.line" and method == "read":
        return [dict(LINEAS[i], id=i) for i in args[0]]
    if model == "ir.model" and method == "read":
        return [{"id": args[0][0], "model": {603: "crm.lead", 499: "sale.order", 1: "account.move"}[args[0][0]]}]
    if method == "search":
        return [1]
    if model == "crm.lead" and method == "read":
        return [{"id": i, "user_id": [ALBA if i == 1 else 7, "u"], "probability": 10, "active": True} for i in args[0]]
    if model == "mail.activity" and method == "read":
        return [{"id": i, "user_id": [ALBA if i == 1 else 7, "u"]} for i in args[0]]
    raise AssertionError(f"llamada inesperada {model}.{method}")


@pytest.fixture
def freno():
    return Freno("backoffice", lambda: ALBA, rpc)


def linea(**kw):
    base = {"product_id": 500, "name": "Taza blanca", "product_uom_qty": 100,
            "price_unit": 3.0, "purchase_price": 2.0}
    base.update(kw)
    return [0, 0, base]


def crear_so(freno, **vals):
    freno.comprobar("sale.order", "create", [vals], {})


# --- lo que SÍ puede -----------------------------------------------------------------

def test_lecturas_siempre(freno):
    for m in ("search_read", "read", "fields_get", "search_count", "check_access_rights"):
        freno.comprobar("account.move", m, [[]], {})


def test_presupuesto_bueno(freno):
    crear_so(freno, partner_id=101, order_line=[linea(), [0, 0, {"display_type": "line_section", "name": "Opción A"}]])


def test_presupuesto_envio_a_coste_no_cuenta_en_margen(freno):
    crear_so(freno, partner_id=100, order_line=[linea(), linea(product_id=30, name="Envío", product_uom_qty=1, price_unit=12, purchase_price=12)])


def test_presupuesto_tramos_cuenta_el_mayor(freno):
    # 3 tramos del mismo producto: 4.500 € el mayor, 9.000 € sumados → pasa
    crear_so(freno, partner_id=100, order_line=[
        linea(product_uom_qty=500, price_unit=3.0), linea(product_uom_qty=1000, price_unit=2.5),
        linea(product_uom_qty=1500, price_unit=3.0)])


def test_margen_ok_exento_de_suelo(freno):
    crear_so(freno, partner_id=300, order_line=[linea(price_unit=2.2)])


def test_aeol_suelo_18(freno):
    crear_so(freno, partner_id=789, order_line=[linea(price_unit=2.5)])  # 20 %


def test_corregir_su_borrador(freno):
    freno.comprobar("sale.order", "write", [[1], {"order_line": [[1, 11, {"price_unit": 3.2}], linea()], "note": "x"}], {})


def test_nota_interna(freno):
    freno.comprobar("mail.message", "create", [{"model": "crm.lead", "res_id": 5, "body": "Llamada", "message_type": "comment", "subtype_id": 2}], {})


def test_actividad_para_ivan(freno):
    freno.comprobar("mail.activity", "create", [{"res_model_id": 603, "res_id": 5, "summary": "Es de Iván", "user_id": 7}], {})


def test_lead_nuevo(freno):
    freno.comprobar("crm.lead", "create", [{"name": "Tazas 200", "email_from": "nuevo@x.com", "contact_name": "Ana"}], {})


def test_contacto_nuevo(freno):
    freno.comprobar("res.partner", "create", [{"name": "Nueva SL", "is_company": True, "email": "hola@nueva.es"}], {})


# --- lo que NO puede -----------------------------------------------------------------

@pytest.mark.parametrize("method", ["unlink", "load", "action_confirm", "action_quotation_send",
                                    "message_post", "copy", "action_feedback", "action_cancel"])
def test_metodos_prohibidos(freno, method):
    with pytest.raises(FrenoError):
        freno.comprobar("sale.order", method, [[1]], {})


@pytest.mark.parametrize("model", ["account.move", "purchase.order", "product.product", "res.users",
                                   "ir.config_parameter", "base.automation", "mail.template", "mail.mail"])
def test_modelos_prohibidos(freno, model):
    with pytest.raises(FrenoError):
        freno.comprobar(model, "create", [{"name": "x"}], {})
    with pytest.raises(FrenoError):
        freno.comprobar(model, "write", [[1], {"name": "x"}], {})


@pytest.mark.parametrize("partner", [200, 201])
def test_cuenta_reservada(freno, partner):
    with pytest.raises(FrenoError, match="reservada"):
        crear_so(freno, partner_id=partner, order_line=[linea()])


@pytest.mark.parametrize("campo,valor", [("state", "sale"), ("tag_ids", [[6, 0, [41]]]),
                                         ("require_signature", True), ("company_id", 2)])
def test_campos_prohibidos(freno, campo, valor):
    with pytest.raises(FrenoError):
        crear_so(freno, partner_id=100, order_line=[linea()], **{campo: valor})


def test_otro_comercial(freno):
    with pytest.raises(FrenoError, match="nombre de Alba"):
        crear_so(freno, partner_id=100, user_id=7, order_line=[linea()])


def test_generico_merchandising(freno):
    with pytest.raises(FrenoError, match="genérico"):
        crear_so(freno, partner_id=100, order_line=[linea(product_id=74)])


def test_sin_coste(freno):
    with pytest.raises(FrenoError, match="purchase_price"):
        crear_so(freno, partner_id=100, order_line=[linea(purchase_price=0)])


def test_bajo_suelo(freno):
    with pytest.raises(FrenoError, match="suelo"):
        crear_so(freno, partner_id=100, order_line=[linea(price_unit=2.5)])  # 20 %


def test_bajo_coste(freno):
    with pytest.raises(FrenoError, match="negativo"):
        crear_so(freno, partner_id=300, order_line=[linea(price_unit=1.9)])  # ni con Margen OK


def test_mas_de_5000(freno):
    with pytest.raises(FrenoError, match="Iván"):
        crear_so(freno, partner_id=100, order_line=[linea(product_uom_qty=1000, price_unit=3.0),
                                                     linea(product_id=501, name="Libreta", product_uom_qty=1000, price_unit=3.0)])


def test_linea_suelta(freno):
    with pytest.raises(FrenoError):
        freno.comprobar("sale.order.line", "create", [{"order_id": 1, "product_id": 500}], {})


@pytest.mark.parametrize("pedido", [2, 3])  # enviado · de Iván
def test_editar_ajeno_o_enviado(freno, pedido):
    with pytest.raises(FrenoError):
        freno.comprobar("sale.order", "write", [[pedido], {"note": "x"}], {})


def test_editar_linea_de_otro_pedido(freno):
    with pytest.raises(FrenoError):
        freno.comprobar("sale.order", "write", [[1], {"order_line": [[1, 999, {"price_unit": 9}]]}], {})


def test_editar_baja_margen(freno):
    with pytest.raises(FrenoError, match="suelo"):
        freno.comprobar("sale.order", "write", [[1], {"order_line": [[1, 11, {"price_unit": 2.4}]]}], {})


def test_vaciar_lineas(freno):
    with pytest.raises(FrenoError):
        freno.comprobar("sale.order", "write", [[1], {"order_line": [[5]]}], {})


def test_nota_que_seria_correo(freno):
    with pytest.raises(FrenoError, match="internas"):
        freno.comprobar("mail.message", "create", [{"model": "crm.lead", "res_id": 5, "body": "Hola", "message_type": "comment", "subtype_id": 1}], {})
    with pytest.raises(FrenoError):
        freno.comprobar("mail.message", "create", [{"model": "crm.lead", "res_id": 5, "body": "Hola", "message_type": "comment", "subtype_id": 2, "partner_ids": [[6, 0, [100]]]}], {})


def test_actividad_sin_modelo_o_en_factura(freno):
    with pytest.raises(FrenoError, match="res_model_id"):
        freno.comprobar("mail.activity", "create", [{"res_model": "crm.lead", "res_id": 5}], {})
    with pytest.raises(FrenoError):
        freno.comprobar("mail.activity", "create", [{"res_model_id": 1, "res_id": 5}], {})


def test_actividad_para_otro(freno):
    with pytest.raises(FrenoError):
        freno.comprobar("mail.activity", "create", [{"res_model_id": 603, "res_id": 5, "user_id": 18}], {})


def test_editar_actividad_ajena(freno):
    with pytest.raises(FrenoError):
        freno.comprobar("mail.activity", "write", [[2], {"note": "x"}], {})


def test_lead_con_etiqueta(freno):
    with pytest.raises(FrenoError, match="tag_ids"):
        freno.comprobar("crm.lead", "create", [{"name": "x", "tag_ids": [[6, 0, [15]]]}], {})


def test_lead_de_cuenta_reservada(freno):
    with pytest.raises(FrenoError, match="reservada"):
        freno.comprobar("crm.lead", "create", [{"name": "x", "email_from": "cecotec@cecotec.es"}], {})


def test_lead_ajeno_o_etapa(freno):
    with pytest.raises(FrenoError):
        freno.comprobar("crm.lead", "write", [[2], {"description": "x"}], {})
    with pytest.raises(FrenoError):
        freno.comprobar("crm.lead", "write", [[1], {"stage_id": 4}], {})


def test_contacto_duplicado(freno):
    with pytest.raises(FrenoError, match="101"):
        freno.comprobar("res.partner", "create", [{"name": "Ya", "email": "ya@existe.com"}], {})


def test_contacto_con_etiqueta(freno):
    with pytest.raises(FrenoError):
        freno.comprobar("res.partner", "create", [{"name": "x", "category_id": [[6, 0, [88]]]}], {})


def test_editar_contacto(freno):
    with pytest.raises(FrenoError):
        freno.comprobar("res.partner", "write", [[100], {"email": "x"}], {})


def test_create_en_lote(freno):
    with pytest.raises(FrenoError):
        freno.comprobar("sale.order", "create", [[{"partner_id": 100, "order_line": [linea()]},
                                                  {"partner_id": 200, "order_line": [linea()]}]], {})


def test_perfil_desconocido():
    with pytest.raises(ValueError):
        Freno("todo", lambda: ALBA, rpc)
