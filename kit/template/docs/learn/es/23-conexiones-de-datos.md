# Conexiones de datos de mercado

Todas las fuentes de datos de mercado se configuran en **Conexiones**, y en ningún otro lugar. Cada llave es tuya. Se guarda en el almacén de credenciales de tu sistema y nunca vuelve a mostrarse.

## Conectar una fuente

1. En el menú, abre **Conexiones**.
2. Busca la tarjeta del proveedor (acciones y ETF de EE. UU., cripto, forex o futuros).
3. Pega tu llave en **Llave de API** y pulsa **Guardar llave**.
4. Pulsa **Probar conexión**.
5. Enciende el interruptor de la fuente.

Debe verse: "Conectado" con el tipo de dato (**En vivo** o **Con retraso**) y los símbolos en uso. Algunas fuentes piden más de un campo, como **ID de la llave** y **Secreto**.

## Prioridad entre fuentes

Si dos fuentes conectadas cubren la misma clase de activo, atiende la primera de la lista. En **Prioridad**, arrastra para cambiar el orden o usa las flechas.

## Elegir qué usa cada mesa

Las conexiones se configuran una vez. En la mesa, abre **Configuración**, sección **Conexiones**, y elige cuáles usa esa mesa.

## Si algo falla

- **"El proveedor no aceptó la llave".** Revísala y pégala completa.
- **"No llegaron datos en 10 segundos".** Intenta más tarde.
- **"El proveedor está limitando las solicitudes".** Espera un minuto.
- **"Una conexión por cuenta".** Cierra la otra conexión y reintenta.
- **"Esta fuente llegó a su límite de símbolos".** Cierra un gráfico o enciende otra fuente.
- **"Almacén de credenciales del sistema no disponible".** Tu equipo no puede guardar llaves. Manda tu `engine.log`.
