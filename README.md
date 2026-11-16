# PhSellStats

Aplicación web local para monitorear, graficar y controlar en un solo tablero tus ventas de fotografía en **Lumepic** y **Momentia**.

## Funcionalidades
- **Revisión periódica automática**: consulta ambas plataformas en segundo plano cada $N$ minutos (configurable desde la interfaz) usando Playwright con sesiones persistentes.
- **Evolución temporal detallada**: gráficos por hora, por día o acumulados (ingresos o cantidad de ventas).
- **Comparativa unificada por evento**: compará ingresos, ticket promedio y velocidad de ventas (ventas/hora) entre eventos y plataformas.
- **Metas y Alertas**: definí objetivos por ingresos o cantidad y recibí alertas en el tablero (y notificaciones del navegador) cuando entren ventas nuevas o alcances una meta.

## Instalación rápida (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Ejecución

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Luego abrí `http://127.0.0.1:8000` en tu navegador.

## Conectar tus cuentas reales
1. En la parte superior derecha hacé clic en **Cuentas y Config**.
2. Hacé clic en **Conectar cuenta** para Lumepic y/o Momentia.
3. Se abrirá una ventana de Chromium: iniciá sesión normalmente (incluyendo 2FA si tu cuenta lo usa) y, cuando veas tu tablero, cerrá esa ventana para guardar la sesión localmente en `data/sessions/`.
4. Si necesitás ajustar las URLs exactas de login o tablero, podés editarlas en el mismo modal o en `.env`.
5. Cuando ya estés usando datos reales, podés borrar los datos de demostración con **Limpiar datos demo**.
