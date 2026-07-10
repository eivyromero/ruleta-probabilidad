# Ruleta de la Probabilidad — Versión Multijugador

Proyecto de Estadística y Probabilidad · Casa Abierta

## Qué es esto
Una ruleta interactiva con **servidor real y base de datos SQLite**, no solo
un archivo HTML. Tú proyectas el tablero principal en una pantalla grande;
tus compañeros escanean un código QR y apuestan desde su propio celular.
Todo se sincroniza en vivo a través de tu laptop.

## Requisitos
- Python 3.9 o superior (ya lo tienes instalado)
- Que los celulares y tu laptop estén en la **misma red WiFi local**
  (no necesitas internet real — puedes usar el hotspot de tu propio celular
  o laptop, solo debe ser la MISMA red para todos)

## Cómo correrlo
1. Abre una terminal en esta carpeta.
2. Instala las dependencias (una sola vez):
   ```
   pip install -r requirements.txt
   ```
3. Enciende el servidor:
   ```
   python server.py
   ```
4. La consola te mostrará algo como:
   ```
   Tablero principal (proyectar):  http://192.168.1.15:5000/
   Página de juego (celulares):     http://192.168.1.15:5000/jugar
   ```
5. Abre la primera URL en la laptop que vas a proyectar — ese es el
   tablero principal con la ruleta grande y el código QR.
6. Tus compañeros escanean el QR (que aparece automáticamente en el
   tablero) con la cámara de su celular, ponen su nombre, y ya pueden
   apostar.
7. Tú (el anfitrión) presionas "Girar la ruleta" en la laptop cuando
   quieras cerrar la ronda de apuestas — la ruleta grande gira, y en
   cada celular aparece automáticamente si ganaron o no.

## Si no tienes WiFi disponible en el lugar
Crea un hotspot personal desde tu propio celular (Ajustes → Punto de
acceso / Hotspot). No necesita tener datos móviles activos para esto —
solo sirve como red local entre los dispositivos. Conecta tu laptop a
ese hotspot y sigue los pasos normales.

## Qué hay debajo del capó (para explicarle al ING)
- **Base de datos relacional (SQLite)** con 3 tablas: `players`, `spins`,
  `bets` — cada apuesta y cada tirada quedan registradas permanentemente.
- **Arquitectura cliente-servidor**: el servidor Flask expone una API
  (`/api/join`, `/api/bet`, `/api/spin`, `/api/stats`) que los celulares
  y el tablero consumen por HTTP.
- **Prueba de hipótesis chi-cuadrado** calculada en el servidor con los
  datos reales acumulados, comparando la distribución observada contra
  la teórica (χ² con 2 grados de libertad, α = 0.05).
- **Múltiples clientes concurrentes**: cualquier número de celulares
  puede apostar al mismo tiempo sin pisarse entre sí, porque las
  transacciones de monedas se resuelven en la base de datos.

## Archivo standalone (respaldo sin servidor)
Si por algo falla la red el día del evento, sigue teniendo disponible
la versión de un solo archivo HTML (sin multijugador) como respaldo.
