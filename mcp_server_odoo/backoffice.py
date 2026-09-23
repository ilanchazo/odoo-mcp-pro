"""Arranque del conector de backoffice: el freno va siempre puesto.

23/09/2026. Es el comando que usa el Claude de Alba (`nomada-backoffice-odoo`). Fuerza
ODOO_MCP_FRENO=backoffice antes de arrancar, así que quitar o cambiar esa variable en la
configuración de Claude Desktop no quita el freno: habría que cambiar el comando.
"""

import os


def main():
    os.environ["ODOO_MCP_FRENO"] = "backoffice"
    from .__main__ import main as arrancar

    return arrancar()


if __name__ == "__main__":
    main()
