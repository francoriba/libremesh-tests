# Instructivo de Uso: Control de Energía y Flasheo de Firmware en Labgrid

Este documento detalla las funcionalidades implementadas en el framework de testing Labgrid para la gestión de energía y el flasheo automatizado de firmware en dispositivos OpenWrt/LibreMesh, con un enfoque en la robustez, la reproducibilidad y la persistencia de la configuración de red.

---

## 1. Introducción

El objetivo de estas funcionalidades es garantizar que los dispositivos bajo prueba (DUTs) puedan ser puestos en un estado conocido y reproducible antes de ejecutar tests, así como recuperarlos de estados "brickeados" o con problemas de conectividad. Esto abarca los casos principales:

*   **Flasheo "Feliz" (Sysupgrade vía SSH)**: Para dispositivos que ya están operativos y permiten acceso SSH.
*   **Recuperación "Extrema" (U-Boot/TFTP)**: Para dispositivos que no arrancan correctamente o no tienen acceso SSH.
*   **Gestión Robusta de Conectividad**: Asegurar el acceso SSH de forma persistente, incluso después de reboots y flasheos.

---

## 2. Flasheo de Firmware (`SysupgradeDriver`)

El `SysupgradeDriver` encapsula la lógica para flashear imágenes de firmware OpenWrt/LibreMesh utilizando el comando `sysupgrade` a través de SSH. Incorpora una serie de **guardas de seguridad** para prevenir el flasheo de imágenes incorrectas o corruptas.

### Capacidades Clave:

*   **Flasheo Básico**: Sube la imagen vía SCP y ejecuta `sysupgrade -n` (sin preservar configuración) o `sysupgrade` (preservando configuración).
*   **Guardas de Imagen Robustas**:
    *   **Compatibilidad de Board**: Verifica que la imagen de firmware sea compatible con el modelo de hardware (`expected_board` configurado en YAML vs. `sysinfo/board_name` del DUT).
    *   **Checksum Local SHA256**: Calcula y valida el SHA256 de la imagen en el host antes de la subida.
    *   **Espacio en `/tmp`**: Comprueba que haya suficiente espacio libre en `/tmp` del DUT para la imagen, con un margen configurable.
    *   **Integridad Remota (Tamaño + SHA256)**: Después de subir la imagen, verifica su tamaño y SHA256 en el DUT para asegurar que no se haya corrompido o truncado durante la transferencia.
    *   **Validación Funcional (`sysupgrade -T`)**: Ejecuta `sysupgrade -T` en el DUT para que el propio dispositivo valide la compatibilidad de la imagen, capturando tanto `stdout` como `stderr` para mejor diagnóstico.
    *   **Advertencia UBI**: Si el board es UBI pero el nombre del archivo de la imagen no contiene "-ubi-", se emite una advertencia.
*   **Idempotencia Opcional (`--flash-skip-if-same`)**: Puede configurarse para saltar el flasheo si se detecta que la misma versión de firmware ya está instalada.
*   **Downgrade Controlado (`allow_downgrade`)**: Por defecto, no se fuerza el flasheo (`-F`). Solo se usa el flag `-F` de `sysupgrade` si `allow_downgrade` se establece explícitamente a `True`.
*   **Modo "Solo Validar" (`--flash-validate-only`)**: Útil para CI/CD. Ejecuta todas las guardas y validaciones (incluyendo `sysupgrade -T`), pero se detiene antes de realizar el flasheo real, devolviendo un éxito si todas las validaciones pasan.

### Parámetros Configurables (en `targets/*.yaml` bajo `SysupgradeDriver`):

*   `keep_config`: `true` o `false` (por defecto `false`). Si `true`, no usa `-n`.
*   `allow_downgrade`: `true` o `false` (por defecto `false`). Si `true`, usa `-F`.
*   `verify_boot_timeout`: Tiempo en segundos para esperar después de un `sysupgrade` para que el dispositivo reinicie.
*   `expected_board`: String, nombre esperado del board (ej. `"linksys,e8450-ubi"`).
*   `skip_if_installed`: `true` o `false` (por defecto `false`).
*   `tmp_space_margin_mb`: Entero, margen adicional en MB para el espacio en `/tmp`.
*   `remote_path`: String, ruta remota temporal en el DUT para la imagen de firmware (por defecto `/tmp/sysupgrade.bin`).
*   `validate_only`: `true` o `false` (por defecto `false`). Habilita el modo "solo validar".

---

## 3. Control de Energía y Recuperación de Conectividad (`PhysicalDeviceStrategy`)

La `PhysicalDeviceStrategy` extiende la funcionalidad base de Labgrid para manejar el ciclo de vida completo de un dispositivo físico, incluyendo el encendido/apagado, la espera del sistema operativo y la recuperación avanzada, con un énfasis especial en asegurar una conectividad SSH robusta y persistente.

### Capacidades Clave:

*   **Encendido/Apagado Automático**:
    *   `strategy.transition("shell")`: Asegura que el dispositivo esté encendido y que el shell de Linux esté accesible. Si el dispositivo está apagado, lo enciende usando el `ExternalPowerDriver` configurado en el YAML.
    *   `strategy.transition("off")` o `strategy.ensure_off()`: Garantiza que el dispositivo esté físicamente apagado.
    *   `strategy.cleanup_and_shutdown()`: Intenta un apagado limpio por SSH y luego apaga físicamente.
*   **Configuración de Red Robusta (DHCP y Persistencia)**:
    *   `strategy.configure_libremesh_network()`: Configura la interfaz LAN del dispositivo (LibreMesh u OpenWrt) para usar DHCP. Esto es crucial para permitir el acceso SSH desde la infraestructura de testeo.
    *   **Prioriza SSH**: Intenta configurar la red vía SSH primero por eficiencia.
    *   **Fallback a Serial**: Si SSH no está disponible (ej. después de un flash limpio o recovery), utiliza la consola serial para enviar los comandos UCI. Se ha mejorado la robustez para manejar problemas de codificación UTF-8.
    *   **Persistencia de Configuración**: Para dispositivos LibreMesh, la estrategia ahora **deshabilita el servicio `lime-config`** para asegurar que la configuración de red (DHCP) persista a través de reboots y no sea sobrescrita por la auto-configuración de LibreMesh.

*   **Verificación Activa de SSH (`_wait_for_ssh_available`)**:
    *   Después de cualquier configuración de red o reboot, la estrategia no asume que SSH estará disponible. En su lugar, **sondea activamente el puerto SSH** hasta que la conexión se establece y responde a un comando de prueba (`echo test`).
    *   Utiliza timeouts configurables (`SSH_AVAILABILITY_CHECK_LIBREMESH`, `SSH_AVAILABILITY_CHECK_OPENWRT`) para adaptarse a los tiempos de inicio del sistema operativo y la red.

*   **Recuperación de SSH vía Serial (`_force_ssh_up_via_serial`)**:
    *   Este es un mecanismo de fallback avanzado que se activa si la verificación activa de SSH falla después de la configuración de red.
    *   **Diagnóstico y Reparación Automatizada**: Realiza los siguientes pasos a través de la consola serial para restaurar el acceso SSH:
        1.  **Detiene y deshabilita permanentemente el servicio `lime-config`**: Previene la interferencia con DHCP y asegura la persistencia de la configuración.
        2.  **Configura las UCI para DHCP persistente**: Asegura que el archivo `/etc/config/network` refleje la configuración de DHCP deseada.
        3.  **Reinicia el servicio de red**: Fuerza al dispositivo a intentar obtener una IP.
        4.  **Espera activamente la asignación de IP por DHCP**: Sondea el estado de la interfaz de red hasta que se detecta una dirección IP válida.
        5.  **Intento de Renovación DHCP Agresivo**: Si no se obtiene una IP, se ejecuta `udhcpc` directamente para forzar la adquisición de una lease.
        6.  **Asegura que `dropbear` (servidor SSH) esté corriendo**: Reinicia el servicio `dropbear`.
        7.  **Verifica nuevamente el acceso SSH**: Intenta conectar vía SSH después de los pasos de recuperación.
    *   Si la recuperación por serial falla, la prueba se detiene con un error claro (`StrategyError`) para evitar ejecuciones con un estado inconsistente.

*   **Recuperación por U-Boot/TFTP (`attempt_uboot_recovery`)**: Mecanismo para "revivir" dispositivos "brickeados" o que no arrancan:
    1.  **Power Cycle**: Apaga y enciende el dispositivo.
    2.  **Interrupción de U-Boot**: Envía secuencias de caracteres por serial para entrar al bootloader U-Boot.
    3.  **TFTP Initramfs**: Carga una imagen `initramfs` (kernel temporal) a la RAM del dispositivo vía TFTP. La imagen y la IP del servidor TFTP se configuran en el YAML del `UBootDriver`.
    4.  **Boot a RAM**: Arranca el sistema desde la imagen `initramfs` cargada en RAM.
    5.  **Persistencia (Sysupgrade)**: Una vez que el sistema initramfs bootea y se obtiene un shell, el driver sube la imagen `sysupgrade` final (la misma que se usa para el flasheo "feliz") al DUT (vía SSH si está disponible, o descargándola por TFTP/serial si no lo está desde la IP del servidor TFTP configurado en U-Boot) y la flashea a la memoria flash del dispositivo con `sysupgrade -n -F`.
    6.  **Reinicio**: Espera a que el dispositivo reinicie con el firmware persistente y se verifique el acceso SSH.

*   **Diferencias Específicas por Dispositivo**:
    *   **GL-iNet (GL-MT300N-V2)**:
        *   Requiere aislamiento serial durante el ciclo de energía mediante `SerialIsolatorDriver` para evitar problemas de boot.
        *   No soporta recuperación vía U-Boot (`enable_uboot_recovery: false`); utiliza un método de recuperación basado en web.
        *   Mayor tiempo de espera después de configuración DHCP (`post_dhcp_wait: 90s`) debido a que LibreMesh tarda más en inicializar la malla (batman-adv) y los servicios SSH.
    *   **Belkin RT3200/Linksys E8450**:
        *   No requiere aislamiento serial (`requires_serial_disconnect: false`).
        *   Soporta recuperación completa vía U-Boot (`enable_uboot_recovery: true`).
        *   Menor tiempo de espera tras configuración DHCP (`post_dhcp_wait: 15s`).
        *   Configuración específica U-Boot para TFTP (prompt, interrupciones, bootfile).

### Parámetros Configurables (en `targets/*.yaml` bajo `PhysicalDeviceStrategy`):

*   `requires_serial_disconnect`: `true` o `false`. Si `true`, activa el `SerialIsolatorDriver` para un ciclo de energía especial.
*   `boot_wait`: Tiempo en segundos para esperar después de encender el dispositivo para que inicie el sistema operativo.
*   `connection_timeout`: Tiempo máximo en segundos para establecer una conexión al shell.
*   `smart_state_detection`: `true` o `false`. Si `true`, intenta detectar si el shell ya está activo antes de hacer un power cycle.
*   `enable_uboot_recovery`: `true` o `false`. Habilita o deshabilita el mecanismo de recuperación U-Boot.
*   `max_recovery_attempts`: Número máximo de intentos de recuperación U-Boot.
*   `tftp_root`: String, ruta absoluta al directorio raíz del servidor TFTP en el host (ej. `"/srv/tftp"`).
*   **Parámetros de U-Boot Recovery (específicos del dispositivo):**
    *   `uboot_interrupt_delay`: Retraso en segundos entre el envío de caracteres de interrupción U-Boot.
    *   `uboot_interrupt_count`: Número de caracteres de interrupción a enviar.
    *   `uboot_power_off_wait`: Tiempo en segundos para esperar después de apagar el dispositivo en un recovery.
    *   `uboot_boot_wait`: Tiempo máximo en segundos para esperar a que el shell de Linux esté disponible después de bootear el initramfs.
*   **Parámetros de Configuración de Red (LibreMesh):**
    *   `post_dhcp_wait`: Tiempo en segundos para esperar después de reiniciar la red para que DHCP obtenga una IP.
    *   `network_config_retry_wait`: Tiempo en segundos de espera entre los comandos de configuración de red enviados por serial.

### Parámetros Configurables (en `targets/*.yaml` bajo `UBootDriver`):

*   `prompt`: String, el prompt esperado del bootloader U-Boot.
*   `autoboot`: String, el mensaje que indica el inicio del autoboot (para saber cuándo interrumpir).
*   `interrupt`: String, el carácter o secuencia para interrumpir U-Boot (ej. `" "` o `"0\\n"`).
*   `init_commands`: Lista de strings, comandos a ejecutar en el prompt de U-Boot (ej. `setenv serverip`, `tftpboot`). Es crítico que `setenv bootfile` apunte a una imagen **initramfs** para el flujo de recovery.
*   `boot_command`: String, el comando final para bootear el kernel después de TFTP.

---

## 4. Integración con Pytest y Makefiles

La integración permite activar estas funcionalidades desde la línea de comandos de Pytest o mediante Makefiles.

### Opciones de Pytest (`pytest_addoption`):

*   `--firmware <path>`: Especifica la ruta a la imagen de firmware local (`sysupgrade.itb`).
*   `--flash-firmware`: Flag para activar el flasheo automático al inicio de la sesión de tests.
*   `--flash-keep-config`: Flag para mantener la configuración existente en el DUT durante el flasheo (usa `sysupgrade` sin `-n`).
*   `--flash-verify-version <version>`: String, versión esperada del firmware para verificar después del flasheo.
*   `--flash-sha256 <hash>`: String, SHA256 esperado de la imagen de firmware para validación.
*   `--flash-skip-if-same`: Flag para saltar el flasheo si la versión ya está instalada.
*   `--flash-validate-only`: Flag para activar el modo "solo validar" (corre todas las guardas sin flashear realmente).

### Uso con Makefiles:

Los Makefiles en el directorio `tests/` están configurados para usar estas opciones:

```bash
# Ejemplo: Flashear Belkin RT3200 con imagen y SHA256 específicos
make tests/belkin_rt3200_1 \
  FIRMWARE=/home/franco/pi/images/lime-mediatek-mt7622-linksys_e8450-ubi-squashfs-sysupgrade.itb \
  FLASH_FIRMWARE=1 \
  FLASH_SHA256=5da052ac528e0ae50d08021ce4e9eaf88a0572379174828e3d2f8219280c819a
```

### Fixtures Útiles:

*   `firmware_image`: Fixture de pytest que provee la ruta a la imagen de firmware configurada.
*   `sysupgrade_driver`: Fixture que da acceso directo a la instancia del `SysupgradeDriver` para operaciones manuales en tests específicos.
*   `flash_clean_firmware`: Fixture que flashea una imagen limpia (sin preservar config) antes de un test, garantizando un estado inicial conocido.

---

## 5. Ejemplos de Uso

*   **Solo ejecutar tests (sin flashear)**:
    ```bash
    pytest tests/my_test.py --lg-env targets/belkin_rt3200_1.yaml
    ```
*   **Validar una imagen sin flashear (modo CI/CD)**:
    ```bash
    pytest tests/test_base.py::test_ubus_system_board -v -s --log-cli-level=INFO \
      --lg-env targets/belkin_rt3200_1.yaml \
      --firmware /home/franco/pi/images/lime-mediatek-mt7622-linksys_e8450-ubi-squashfs-sysupgrade.itb \
      --flash-firmware --flash-validate-only \
      --flash-sha256 5da052ac528e0ae50d08021ce4e9eaf88a0572379174828e3d2f8219280c819a
    ```
*   **Flashear un dispositivo con la imagen por defecto y luego ejecutar tests**:
    ```bash
    make tests/belkin_rt3200_1 FLASH_FIRMWARE=1
    ```
*   **Flashear con imagen específica, preservar config y verificar versión**:
    ```bash
    pytest tests/my_test.py --lg-env targets/belkin_rt3200_1.yaml \
      --firmware /home/franco/pi/images/lime-mediatek-mt7622-linksys_e8450-ubi-squashfs-sysupgrade.itb \
      --flash-firmware --flash-keep-config --flash-verify-version "23.05.5-512e76967f"
    ```
*   **Forzar un recovery U-Boot manual (para pruebas de recovery)**:
    ```bash
    pytest tests/test_uboot_recovery.py::test_uboot_recovery_manual -v -s --log-cli-level=INFO \
      --lg-env targets/belkin_rt3200_1.yaml \
      --firmware /home/franco/pi/images/lime-mediatek-mt7622-linksys_e8450-ubi-squashfs-sysupgrade.itb
    ```

---

## 6. Desarrollos Futuros (Próximos Pasos)

*   **Provisioning Multi-DUT**: Crear una estrategia de `MeshProvisionStrategy` y un marcador de Pytest para provisionar múltiples routers en paralelo, optimizando tests de red mallada.
*   **Artefactos y Trazabilidad en CI**: Generar logs y archivos de estado del dispositivo (`serial.log`, `sysupgrade.log`, `uboot.log`, `openwrt_release`, `board.json`, `dmesg`, `sha256.txt`) como artefactos de CI para facilitar la depuración y auditoría.
*   **Selección de Método de Flasheo**: Un flag `--flash-method=auto|sysupgrade|uboot` para controlar explícitamente el método de flasheo.

---