"""Freno de Nómada: lo que un Claude del equipo puede y no puede hacer en Odoo.

23/09/2026. Se activa con ``ODOO_MCP_FRENO=backoffice`` en la configuración del
conector. Sin esa variable el conector se comporta exactamente igual que antes.

El freno vive en ``OdooConnection.execute_kw``, el único sitio por el que pasa TODA
llamada a Odoo: ninguna herramienta del MCP, presente o futura, puede saltárselo.

Principio: lo que no está permitido aquí, está prohibido. El freno no sustituye a
los permisos de Odoo (reglas 466-471 de Alba, que siguen mandando por debajo):
los estrecha. Su trabajo no es frenar a la persona —ella tiene la pantalla—, sino
frenar los errores de su Claude.

Perfil ``backoffice`` (Alba):
- Lee todo lo que su usuario ve en pantalla.
- Crea presupuestos en BORRADOR, a su nombre, con las líneas dentro, y los corrige
  mientras sigan en borrador y sean suyos. Nunca los confirma, envía ni borra.
- Deja notas internas y actividades. Nunca manda un correo.
- Registra leads y contactos nuevos, sin etiquetas (la etiqueta Catálogo dispara
  un correo al cliente) y sin duplicar un contacto que ya existe.
- Nunca borra nada, nunca importa, nunca llama a un método de Odoo.

Los frenos de negocio (cuentas reservadas, producto genérico, coste, margen, techo
de importe) son los de ``reglas-comerciales.md`` y de la ficha de backoffice; si
cambian allí, se cambian aquí.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .error_handling import ValidationError

logger = logging.getLogger(__name__)


class FrenoError(ValidationError):
    """Operación parada por el freno. El mensaje va tal cual a Claude."""

    def __init__(self, message: str):
        super().__init__(f"FRENO: {message}")


# --- Datos de Odoo (verificados en producción el 23/09/2026) ---------------------

ETIQUETA_CUENTA_IVAN = 90  # res.partner.category: las 46 cuentas de las cajas 1-3
ETIQUETA_MARGEN_OK = 88  # res.partner.category: cuenta exenta del suelo de margen
PRODUCTOS_GENERICOS_MERCH = {39, 34, 32, 74}  # «MERCHANDISING*» y variantes
PRODUCTOS_ENVIO = {30, 75, 51, 23, 608, 76}  # van a coste: fuera del margen
PARTNERS_AEOL = {789, 1212, 18852, 20659, 22212, 1258}
SUBTIPO_NOTA = 2  # mail.message.subtype «Note» (interna)
USUARIO_IVAN = 7
USUARIO_GABRIELA = 6

SUELO_MARGEN = 0.25  # reglas-comerciales §1.1, suelo absoluto no-AEOL
SUELO_MARGEN_AEOL = 0.18  # §1.5
TECHO_IMPORTE = 5000.0  # ficha de backoffice: más de 5.000 € es de Iván

METODOS_LECTURA = {
    "search",
    "search_read",
    "read",
    "search_count",
    "fields_get",
    "check_access_rights",
    "name_search",
    "read_group",
    "default_get",
}

MODELOS_CON_CHATTER = {"crm.lead", "sale.order", "res.partner"}

CAMPOS_PRESUPUESTO = {
    "partner_id",
    "partner_invoice_id",
    "partner_shipping_id",
    "order_line",
    "note",
    "validity_date",
    "client_order_ref",
    "opportunity_id",
    "origin",
    "payment_term_id",
    "user_id",
    "x_plazo_entrega_dias",
    "x_entrega_modo",
}
CAMPOS_LINEA = {
    "product_id",
    "name",
    "product_uom_qty",
    "price_unit",
    "purchase_price",
    "discount",
    "display_type",
    "sequence",
}
CAMPOS_LEAD_CREAR = {
    "name",
    "type",
    "partner_id",
    "partner_name",
    "contact_name",
    "email_from",
    "phone",
    "description",
    "user_id",
}
CAMPOS_LEAD_EDITAR = {
    "partner_id",
    "partner_name",
    "contact_name",
    "email_from",
    "phone",
    "description",
}
CAMPOS_CONTACTO = {
    "name",
    "is_company",
    "parent_id",
    "type",
    "email",
    "phone",
    "mobile",
    "function",
    "street",
    "street2",
    "zip",
    "city",
    "state_id",
    "country_id",
    "vat",
    "website",
}
CAMPOS_ACTIVIDAD_CREAR = {
    "res_model",
    "res_model_id",
    "res_id",
    "activity_type_id",
    "summary",
    "note",
    "date_deadline",
    "user_id",
}
CAMPOS_ACTIVIDAD_EDITAR = {"summary", "note", "date_deadline"}
CAMPOS_NOTA = {"model", "res_id", "body", "message_type", "subtype_id"}


def _ids(value: Any) -> List[int]:
    if isinstance(value, int):
        return [value]
    return [int(v) for v in value]


def _m2o(value: Any) -> Optional[int]:
    """Id de un many2one leído ([id, nombre]) o escrito (id)."""
    if isinstance(value, (list, tuple)):
        return int(value[0]) if value else None
    return int(value) if value else None


class Freno:
    """Comprueba cada llamada a Odoo contra el perfil antes de enviarla."""

    PERFILES = {"backoffice"}

    def __init__(self, perfil: str, uid_fn: Callable[[], Optional[int]], rpc: Callable):
        if perfil not in self.PERFILES:
            raise ValueError(
                f"ODOO_MCP_FRENO={perfil!r} no existe. Perfiles: {sorted(self.PERFILES)}"
            )
        self.perfil = perfil
        self._uid_fn = uid_fn
        # rpc(model, method, args, kwargs): llamada a Odoo que NO pasa por el freno.
        # Solo se usa para leer lo que el freno necesita comprobar.
        self._rpc = rpc

    # --- punto de entrada -------------------------------------------------------

    def comprobar(self, model: str, method: str, args: Sequence[Any], kwargs: Dict) -> None:
        if method in METODOS_LECTURA:
            return
        if method == "create":
            vals_list = args[0] if args else kwargs.get("vals_list")
            if isinstance(vals_list, dict):
                vals_list = [vals_list]
            for vals in vals_list or []:
                self._crear(model, vals)
            return
        if method == "write":
            ids = _ids(args[0]) if args else _ids(kwargs.get("ids", []))
            vals = args[1] if len(args) > 1 else kwargs.get("vals", {})
            self._editar(model, ids, vals)
            return
        if method == "unlink":
            raise FrenoError(
                "no se borra nada desde Claude. Si hay que borrarlo, lo hace Alba en "
                "pantalla o se lo pide a Iván."
            )
        if method == "load":
            raise FrenoError("las importaciones masivas no están permitidas desde Claude.")
        raise FrenoError(
            f"el método «{method}» no está permitido desde Claude. Confirmar, enviar, "
            "cancelar o marcar como hecho se hace en pantalla."
        )

    # --- crear ------------------------------------------------------------------

    def _crear(self, model: str, vals: Dict[str, Any]) -> None:
        if model == "sale.order":
            return self._crear_presupuesto(vals)
        if model == "crm.lead":
            return self._crear_lead(vals)
        if model == "res.partner":
            return self._crear_contacto(vals)
        if model == "mail.activity":
            return self._crear_actividad(vals)
        if model == "mail.message":
            return self._crear_nota(vals)
        if model == "sale.order.line":
            raise FrenoError(
                "las líneas se crean dentro del presupuesto (order_line con [0, 0, {…}]), "
                "no sueltas."
            )
        raise FrenoError(f"desde Claude no se crean registros de «{model}».")

    def _crear_presupuesto(self, vals: Dict[str, Any]) -> None:
        self._solo_campos(vals, CAMPOS_PRESUPUESTO, "el presupuesto")
        self._usuario_propio(vals)
        partner = vals.get("partner_id")
        if not partner:
            raise FrenoError("el presupuesto necesita cliente (partner_id).")
        comercial = self._comprobar_cuenta(partner)
        lineas = []
        for cmd in vals.get("order_line") or []:
            if not isinstance(cmd, (list, tuple)) or not cmd or cmd[0] != 0:
                raise FrenoError(
                    "al crear un presupuesto las líneas van solo como [0, 0, {…}]."
                )
            self._comprobar_linea(cmd[2], nueva=True)
            lineas.append(dict(cmd[2]))
        self._comprobar_importes(lineas, comercial)

    def _crear_lead(self, vals: Dict[str, Any]) -> None:
        self._solo_campos(vals, CAMPOS_LEAD_CREAR, "el lead")
        self._usuario_propio(vals)
        if vals.get("partner_id"):
            self._comprobar_cuenta(vals["partner_id"])
        email = (vals.get("email_from") or "").strip()
        if email:
            existentes = self._rpc(
                "res.partner",
                "search_read",
                [[["email", "=ilike", email]]],
                {"fields": ["id", "name", "commercial_partner_id"], "limit": 1},
            )
            if existentes and self._es_cuenta_reservada(
                _m2o(existentes[0]["commercial_partner_id"])
            ):
                raise FrenoError(
                    "ese correo es de una cuenta reservada de Iván. Esto es de Iván: "
                    "no se registra ni se contesta, se le avisa."
                )

    def _crear_contacto(self, vals: Dict[str, Any]) -> None:
        self._solo_campos(vals, CAMPOS_CONTACTO, "el contacto")
        if vals.get("parent_id"):
            self._comprobar_cuenta(vals["parent_id"])
        for campo, etiqueta in (("email", "correo"), ("vat", "CIF")):
            valor = (vals.get(campo) or "").strip()
            if not valor:
                continue
            existe = self._rpc(
                "res.partner",
                "search_read",
                [[[campo, "=ilike", valor]]],
                {"fields": ["id", "display_name"], "limit": 1, "context": {"active_test": False}},
            )
            if existe:
                raise FrenoError(
                    f"ya existe un contacto con ese {etiqueta}: «{existe[0]['display_name']}» "
                    f"(id {existe[0]['id']}). Usa ese, no se duplica."
                )

    def _crear_actividad(self, vals: Dict[str, Any]) -> None:
        self._solo_campos(vals, CAMPOS_ACTIVIDAD_CREAR, "la actividad")
        uid = self._uid()
        destinatario = vals.get("user_id", uid)
        if destinatario not in (uid, USUARIO_IVAN, USUARIO_GABRIELA):
            raise FrenoError("las actividades van para Alba, Iván o Gabriela.")
        if not vals.get("res_model_id") or not vals.get("res_id"):
            raise FrenoError(
                "la actividad necesita res_model_id y res_id; sin res_model_id se pierde "
                "(pasó el 10/09)."
            )
        modelo = self._rpc("ir.model", "read", [[vals["res_model_id"]]], {"fields": ["model"]})
        nombre = modelo[0]["model"] if modelo else None
        if nombre not in MODELOS_CON_CHATTER:
            raise FrenoError("las actividades van en leads, presupuestos o contactos.")
        self._legible(nombre, vals["res_id"])

    def _crear_nota(self, vals: Dict[str, Any]) -> None:
        self._solo_campos(vals, CAMPOS_NOTA, "la nota")
        if vals.get("message_type") != "comment" or vals.get("subtype_id") != SUBTIPO_NOTA:
            raise FrenoError(
                f"solo notas internas: message_type='comment' y subtype_id={SUBTIPO_NOTA}. "
                "Un correo al cliente lo envía Alba desde Odoo."
            )
        if vals.get("model") not in MODELOS_CON_CHATTER or not vals.get("res_id"):
            raise FrenoError("la nota va en un lead, un presupuesto o un contacto.")
        self._legible(vals["model"], vals["res_id"])

    # --- editar -----------------------------------------------------------------

    def _editar(self, model: str, ids: List[int], vals: Dict[str, Any]) -> None:
        if not ids:
            return
        if model == "sale.order":
            return self._editar_presupuesto(ids, vals)
        if model == "crm.lead":
            self._solo_campos(vals, CAMPOS_LEAD_EDITAR, "el lead")
            if vals.get("partner_id"):
                self._comprobar_cuenta(vals["partner_id"])
            leads = self._rpc(
                "crm.lead", "read", [ids], {"fields": ["user_id", "probability", "active"]}
            )
            for lead in leads:
                if _m2o(lead["user_id"]) != self._uid() or not lead["active"]:
                    raise FrenoError(
                        f"el lead {lead['id']} no es de Alba o está cerrado: se toca en pantalla."
                    )
                if lead["probability"] >= 100:
                    raise FrenoError(f"el lead {lead['id']} está ganado: no se toca.")
            return
        if model == "mail.activity":
            self._solo_campos(vals, CAMPOS_ACTIVIDAD_EDITAR, "la actividad")
            for act in self._rpc("mail.activity", "read", [ids], {"fields": ["user_id"]}):
                if _m2o(act["user_id"]) != self._uid():
                    raise FrenoError(
                        f"la actividad {act['id']} no es de Alba: no se toca."
                    )
            return
        if model == "sale.order.line":
            raise FrenoError(
                "las líneas se corrigen desde el presupuesto (order_line con [1, id, {…}])."
            )
        raise FrenoError(f"desde Claude no se modifican registros de «{model}».")

    def _editar_presupuesto(self, ids: List[int], vals: Dict[str, Any]) -> None:
        self._solo_campos(vals, CAMPOS_PRESUPUESTO, "el presupuesto")
        self._usuario_propio(vals)
        pedidos = self._rpc(
            "sale.order",
            "read",
            [ids],
            {"fields": ["name", "state", "user_id", "partner_id", "order_line"]},
        )
        if len(pedidos) != len(set(ids)):
            raise FrenoError("algún presupuesto no existe o Alba no lo ve.")
        for p in pedidos:
            if p["state"] != "draft":
                raise FrenoError(
                    f"{p['name']} ya no está en borrador: se toca en pantalla, no desde Claude."
                )
            if _m2o(p["user_id"]) != self._uid():
                raise FrenoError(
                    f"{p['name']} no es de Alba: Claude solo corrige sus presupuestos."
                )
            partner = vals.get("partner_id") or _m2o(p["partner_id"])
            comercial = self._comprobar_cuenta(partner)
            if "order_line" in vals:
                lineas = self._simular_lineas(p, vals["order_line"])
                self._comprobar_importes(lineas, comercial)

    def _simular_lineas(self, pedido: Dict, comandos: Iterable) -> List[Dict]:
        """Aplica los comandos x2many sobre las líneas actuales, sin escribir nada."""
        actuales = {}
        if pedido["order_line"]:
            for l in self._rpc(
                "sale.order.line",
                "read",
                [pedido["order_line"]],
                {"fields": sorted(CAMPOS_LINEA)},
            ):
                l = dict(l)
                l["product_id"] = _m2o(l.get("product_id"))
                actuales[l["id"]] = l
        nuevas = []
        for cmd in comandos or []:
            if not isinstance(cmd, (list, tuple)) or not cmd:
                raise FrenoError("order_line lleva un comando que no se entiende.")
            op = cmd[0]
            if op == 0:
                self._comprobar_linea(cmd[2], nueva=True)
                nuevas.append(dict(cmd[2]))
            elif op == 1:
                if cmd[1] not in actuales:
                    raise FrenoError(f"la línea {cmd[1]} no es de {pedido['name']}.")
                self._comprobar_linea(cmd[2], nueva=False)
                actuales[cmd[1]].update(cmd[2])
            elif op == 2:
                if cmd[1] not in actuales:
                    raise FrenoError(f"la línea {cmd[1]} no es de {pedido['name']}.")
                actuales.pop(cmd[1])
            else:
                raise FrenoError(
                    "en order_line solo valen [0, 0, {…}] (nueva), [1, id, {…}] (corregir) "
                    "y [2, id] (quitar)."
                )
        return list(actuales.values()) + nuevas

    # --- frenos de negocio ------------------------------------------------------

    def _comprobar_linea(self, vals: Dict[str, Any], nueva: bool) -> None:
        self._solo_campos(vals, CAMPOS_LINEA, "la línea")
        if vals.get("display_type"):
            return  # sección o nota: no lleva precio
        producto = vals.get("product_id")
        if nueva and not producto:
            raise FrenoError("cada línea necesita su producto (product_id).")
        if producto in PRODUCTOS_GENERICOS_MERCH:
            raise FrenoError(
                "producto genérico MERCHANDISING: en merch va el producto real de Makito "
                "con su color y su foto."
            )
        if nueva or "purchase_price" in vals:
            coste = vals.get("purchase_price")
            if not coste or coste <= 0:
                raise FrenoError(
                    "la línea necesita el coste real en purchase_price: sin él el margen "
                    "de Odoo es falso."
                )
        if nueva or "price_unit" in vals:
            if not vals.get("price_unit") or vals["price_unit"] <= 0:
                raise FrenoError("la línea necesita precio de venta (price_unit).")
        if not 0 <= (vals.get("discount") or 0) < 100:
            raise FrenoError("descuento fuera de rango.")

    def _comprobar_importes(self, lineas: List[Dict], comercial: int) -> None:
        venta = coste = 0.0
        subtotales = []
        firmas = set()
        for l in lineas:
            if l.get("display_type"):
                continue
            qty = float(l.get("product_uom_qty") or 1)
            neto = float(l.get("price_unit") or 0) * (1 - float(l.get("discount") or 0) / 100)
            subtotales.append(qty * neto)
            if _m2o(l.get("product_id")) in PRODUCTOS_ENVIO:
                continue
            firmas.add((_m2o(l.get("product_id")), (l.get("name") or "").strip()))
            unit_coste = float(l.get("purchase_price") or 0)
            if neto < unit_coste:
                raise FrenoError(
                    f"la línea «{(l.get('name') or '')[:40]}» vende por debajo del coste. "
                    "Margen negativo: esto es de Iván."
                )
            venta += qty * neto
            coste += qty * unit_coste
        # Un presupuesto de tramos (mismo producto y descripción, varias cantidades)
        # cuenta por su tramo mayor; cualquier otro, entero (misma idea que x_importe_min).
        importe = max(subtotales, default=0) if len(firmas) == 1 and len(subtotales) > 1 else sum(subtotales)
        if importe > TECHO_IMPORTE:
            raise FrenoError(
                f"el presupuesto pasa de {TECHO_IMPORTE:,.0f} € ({importe:,.2f} €). "
                "Esto es de Iván: prepárale el resumen en tres líneas."
            )
        if venta <= 0 or self._tiene_margen_ok(comercial):
            return
        suelo = SUELO_MARGEN_AEOL if comercial in PARTNERS_AEOL else SUELO_MARGEN
        margen = (venta - coste) / venta
        if margen < suelo - 1e-9:
            raise FrenoError(
                f"margen {margen:.1%}, por debajo del suelo del {suelo:.0%}. No se baja: "
                "esto es de Iván."
            )

    def _comprobar_cuenta(self, partner_id: Any) -> int:
        """Devuelve la empresa comercial; para si es una cuenta reservada de Iván."""
        pid = _m2o(partner_id)
        leido = self._rpc(
            "res.partner", "read", [[pid]], {"fields": ["commercial_partner_id"]}
        )
        if not leido:
            raise FrenoError(f"el contacto {pid} no existe o Alba no lo ve.")
        comercial = _m2o(leido[0]["commercial_partner_id"]) or pid
        if self._es_cuenta_reservada(comercial):
            raise FrenoError(
                "es una cuenta reservada de Iván (cajas 1-3). Esto es de Iván: no se "
                "presupuesta ni se toca, se le avisa."
            )
        return comercial

    def _es_cuenta_reservada(self, comercial: Optional[int]) -> bool:
        return bool(comercial) and ETIQUETA_CUENTA_IVAN in self._etiquetas(comercial)

    def _tiene_margen_ok(self, comercial: int) -> bool:
        return ETIQUETA_MARGEN_OK in self._etiquetas(comercial)

    def _etiquetas(self, partner_id: int) -> List[int]:
        leido = self._rpc("res.partner", "read", [[partner_id]], {"fields": ["category_id"]})
        return list(leido[0]["category_id"]) if leido else []

    # --- utilidades -------------------------------------------------------------

    def _uid(self) -> int:
        uid = self._uid_fn()
        if not uid:
            raise FrenoError("sin usuario autenticado: no se escribe nada.")
        return uid

    def _usuario_propio(self, vals: Dict[str, Any]) -> None:
        if "user_id" in vals and vals["user_id"] != self._uid():
            raise FrenoError(
                "desde Claude todo va a nombre de Alba; el reparto de comercial lo decide Iván."
            )

    def _legible(self, model: str, res_id: int) -> None:
        if not self._rpc(model, "search", [[["id", "=", res_id]]], {"limit": 1}):
            raise FrenoError(f"{model} {res_id} no existe o Alba no lo ve.")

    @staticmethod
    def _solo_campos(vals: Dict[str, Any], permitidos: set, que: str) -> None:
        sobran = sorted(set(vals) - permitidos)
        if sobran:
            raise FrenoError(
                f"en {que} Claude no puede tocar: {', '.join(sobran)}. Eso se hace en pantalla."
            )
