import asyncio
import sys

from app.scheduler import get_scraper


async def main() -> None:
    targets = sys.argv[1:] if len(sys.argv) > 1 else ["lumepic", "momentia"]
    for plat in targets:
        if plat not in ("lumepic", "momentia"):
            print(f"Plataforma desconocida: {plat}")
            continue
        print(f"\n=== Conectando {plat.upper()} ===")
        print("Iniciá sesión en la ventana de Brave que se abre y cerrala cuando veas tus ventas.")
        ok = await get_scraper(plat).interactive_login(timeout_seconds=300)
        print(f"Resultado {plat}: {'CONECTADO' if ok else 'NO GUARDADO'}")


if __name__ == "__main__":
    asyncio.run(main())
