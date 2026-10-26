"""Envía un aviso por Telegram con la configuración del bot (.env). Lo usan las tareas programadas del servidor.
Uso:  python despliegue/avisos.py "texto del aviso"
Si Telegram no está configurado o falla, termina sin error (el aviso queda en el registro del sistema)."""

import os
import sys

AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AQUI)


def enviar(texto):
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(AQUI, ".env"))
    except ImportError:
        pass
    from notificaciones import Telegram
    tg = Telegram(os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))
    if not tg.activo:
        print("Telegram no configurado:", texto)
        return False
    try:
        tg._llamar("sendMessage", chat_id=tg.chat_id, text=texto[:4000], disable_web_page_preview=True)
        return True
    except Exception as e:  # noqa: BLE001
        print("No se pudo enviar el aviso:", type(e).__name__)
        return False


if __name__ == "__main__":
    enviar(" ".join(sys.argv[1:]) or "Aviso del servidor del bot.")
