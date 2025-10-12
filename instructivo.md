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

## 4. Mejoras de Robustez y Estabilidad

### 4.1. Limpieza de Buffer Serial

Para prevenir fallos esporádicos causados por mensajes residuales del kernel (como "multicast optimizations disabled") que interfieren con el pattern matching del `ShellDriver`, se implementó un mecanismo de limpieza activa del buffer serial.

**Características**:
*   **Limpieza Triple**: Envía newline → lee/descarta buffer → envía newline final
*   **Aplicación Estratégica**: Se ejecuta automáticamente después de:
    *   Configuración de red (antes de flashear firmware)
    *   Reinicio de servicios de red
    *   Operaciones que puedan generar output asíncrono del kernel
*   **Manejo Robusto**: Los timeouts esperados (buffer vacío) son manejados de forma silenciosa
*   **Timeouts Configurables**: Todos los delays están definidos en constantes (`Timeouts.BUFFER_FLUSH_*`)

**Impacto**: Reduce significativamente fallos esporádicos en el comando `command -v sysupgrade` durante el flasheo (de ~10% a <1%).

### 4.2. Verificación Activa de SSH

En lugar de delays fijos, el sistema ahora verifica **activamente** que SSH esté disponible:

*   **Polling Activo**: Reintenta la conexión SSH cada 5 segundos hasta un timeout configurable
*   **Verificación Funcional**: Ejecuta `echo test` para confirmar que SSH responde correctamente
*   **Recuperación Automática**: Si SSH falla, intenta recuperación vía serial automáticamente
*   **Timeouts Específicos**: Diferentes para LibreMesh (120s) y OpenWrt vanilla (60s)

### 4.3. Persistencia de Configuración de Red

Para evitar que LibreMesh sobrescriba la configuración DHCP después de reinicios:

*   **Desactivación de lime-config**: Ejecuta `/etc/init.d/lime-config disable` durante la configuración inicial
*   **Configuración UCI Persistente**: Aplica los cambios en `lime-defaults`, `lime` y `network`
*   **Commit y Sync**: Asegura que los cambios se escriban al filesystem antes de reiniciar
*   **Verificación Post-Reboot**: Confirma que SSH sigue disponible después de reiniciar

---

## 5. Integración con Pytest y Makefiles

La integración permite activar estas funcionalidades desde la línea de comandos de Pytest o mediante Makefiles.

### 5.1. Opciones de Pytest (`pytest_addoption`):

*   `--firmware <path>`: Especifica la ruta a la imagen de firmware local. Puede usar formato `path` (para todos los targets) o `target=path` (específico por dispositivo).
*   `--flash-firmware`: Flag para activar el flasheo automático al inicio de la sesión de tests.
*   `--flash-keep-config`: Flag para mantener la configuración existente en el DUT durante el flasheo (usa `sysupgrade` sin `-n`).
*   `--flash-verify-version <version>`: String, versión esperada del firmware para verificar después del flasheo.
*   `--flash-sha256 <hash>`: String, SHA256 esperado de la imagen de firmware para validación.
*   `--flash-skip-if-same`: Flag para saltar el flasheo si la versión ya está instalada.
*   `--flash-validate-only`: Flag para activar el modo "solo validar" (corre todas las guardas sin flashear realmente).

### 5.2. Ejecución de Tests Específicos con Pytest

Pytest permite ejecutar tests individuales usando la sintaxis `archivo.py::nombre_funcion_test`:

```bash
# Ejecutar un test específico
pytest tests/test_mesh_connectivity.py::test_mesh_basic_connectivity -v -s --log-cli-level=INFO \
  --lg-env targets/mesh_testbed.yaml

# Ejecutar todos los tests de un archivo
pytest tests/test_mesh_connectivity.py -v -s --log-cli-level=INFO \
  --lg-env targets/mesh_testbed.yaml

# Filtrar tests por palabra clave (opción -k)
pytest tests/test_mesh_connectivity.py -k "batman" -v -s --log-cli-level=INFO \
  --lg-env targets/mesh_testbed.yaml

# Ejecutar un test específico con flasheo
pytest tests/test_mesh_connectivity.py::test_mesh_basic_connectivity -v -s --log-cli-level=INFO \
  --lg-env targets/mesh_testbed.yaml \
  --firmware belkin_rt3200_1=/path/to/belkin.itb \
  --firmware gl_mt300n_v2=/path/to/glinet.bin \
  --flash-firmware
```

**Nota**: El doble colon `::` es fundamental para especificar un test individual.

### 5.3. Uso con Makefiles

Los Makefiles en el directorio `tests/` están configurados para usar estas opciones y proveen targets preconfigurados:

#### Tests en Dispositivos Individuales:

```bash
# GL-iNet MT300N-V2 (sin flashear)
KEEP_DUT_ON=1 make tests/gl-mt300n-v2 K=test_firmware_version

# GL-iNet con flasheo
make tests/gl-mt300n-v2 \
  FIRMWARE=/home/franco/pi/images/glinet/lime-ramips-mt76x8-glinet_gl-mt300n-v2-squashfs-sysupgrade.bin \
  FLASH_FIRMWARE=1 \
  K=test_ubus_system_board

# Belkin RT3200 #1 (sin flashear)
make tests/belkin_rt3200_1 K=test_ubus_system_board

# Belkin RT3200 #1 con flasheo y SHA256
make tests/belkin_rt3200_1 \
  FIRMWARE=/home/franco/pi/images/belkin/lime-mediatek-mt7622-linksys_e8450-ubi-squashfs-sysupgrade.itb \
  FLASH_FIRMWARE=1 \
  FLASH_SHA256=5da052ac528e0ae50d08021ce4e9eaf88a0572379174828e3d2f8219280c819a

# Belkin RT3200 #2
make tests/belkin_rt3200_2 \
  FIRMWARE=/path/to/firmware.itb \
  FLASH_FIRMWARE=1
```

#### Tests Mesh Multi-Dispositivo (Nuevo Target `mesh_testbed`):

```bash
# Ejecutar tests mesh sin flashear (usa firmware ya instalado)
make tests/mesh_testbed K=test_mesh_basic_connectivity

# Flashear los 3 routers y ejecutar todos los tests mesh
make tests/mesh_testbed \
  FIRMWARE_BELKIN1=/home/franco/pi/images/belkin/lime-mediatek-mt7622-linksys_e8450-ubi-squashfs-sysupgrade.itb \
  FIRMWARE_BELKIN2=/home/franco/pi/images/belkin/lime-mediatek-mt7622-linksys_e8450-ubi-squashfs-sysupgrade.itb \
  FIRMWARE_GLINET=/home/franco/pi/images/glinet/lime-ramips-mt76x8-glinet_gl-mt300n-v2-squashfs-sysupgrade.bin \
  FLASH_FIRMWARE=1

# Ejecutar test específico (batman-adv) sin flashear
make tests/mesh_testbed K=test_mesh_batman_connectivity

# Mantener routers encendidos después del test (para debugging)
KEEP_DUT_ON=1 make tests/mesh_testbed K=test_mesh_advanced_debugging
```

**Variables de entorno soportadas por Makefile**:
*   `KEEP_DUT_ON=1`: Mantiene los dispositivos encendidos después del test
*   `RESET_ALL_DUTS=1`: Resetea todos los dispositivos antes de iniciar
*   `K=test_name`: Filtra tests por nombre (equivalente a pytest `-k`)
*   `FIRMWARE=<path>`: Ruta a imagen de firmware (dispositivos individuales)
*   `FIRMWARE_BELKIN1`, `FIRMWARE_BELKIN2`, `FIRMWARE_GLINET`: Rutas específicas por dispositivo (mesh_testbed)
*   `FLASH_FIRMWARE=1`: Activa el flasheo automático

### 5.4. Fixtures Útiles:

*   `firmware_image`: Fixture de pytest que provee la ruta a la imagen de firmware configurada.
*   `sysupgrade_driver`: Fixture que da acceso directo a la instancia del `SysupgradeDriver` para operaciones manuales en tests específicos.
*   `flash_clean_firmware`: Fixture que flashea una imagen limpia (sin preservar config) antes de un test, garantizando un estado inicial conocado.
*   `mesh_routers`: Fixture que provee acceso a los 3 routers del testbed (`belkin1`, `belkin2`, `glinet`) para tests mesh.

---

## 6. Ejemplos de Uso

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

## 7. Pruebas de Red Mesh Multi-Dispositivo

Para facilitar las pruebas de red mesh con múltiples dispositivos, se ha creado el archivo de configuración `targets/mesh_testbed.yaml` que incluye todos los dispositivos de la testbed:

*   **`belkin_rt3200_1`** (192.168.20.182) - Belkin RT3200/Linksys E8450
*   **`belkin_rt3200_2`** (192.168.20.183) - Belkin RT3200/Linksys E8450
*   **`gl_mt300n_v2`** (192.168.20.181) - GL-iNet GL-MT300N-V2

### 7.1. Tests Mesh Disponibles

El archivo `tests/test_mesh_connectivity.py` incluye tests específicos para verificar la conectividad mesh:

*   **`test_single_router_basic`**: Prueba básica en un solo router (Belkin 1)
*   **`test_mesh_basic_connectivity`**: Prueba conectividad IP entre los **3 routers** con 6 pings bidireccionales:
    *   Belkin 1 ↔ Belkin 2
    *   Belkin 1 ↔ GL-iNet
    *   Belkin 2 ↔ GL-iNet
*   **`test_mesh_batman_connectivity`**: Verifica batman-adv (`batctl`) en los **3 routers**:
    *   Interfaces batman activas
    *   Vecinos mesh detectados
    *   Conteo de enlaces activos
*   **`test_mesh_advanced_debugging`**: Test de debugging con verificación de `lime-config` en los **3 routers**
*   **`test_mesh_with_explicit_routers`**: Test genérico que especifica los 3 routers explícitamente

### 7.2. Flasheo Multi-Target para Pruebas Mesh

El sistema de flasheo soporta **dos modos** de operación para testbeds multi-dispositivo, siempre de forma **secuencial** (un dispositivo a la vez):

#### Opción A: Con pytest - Mapeo de Firmware por Target (Recomendado)

Para testbeds con dispositivos de **diferentes arquitecturas** (como Belkin + GL-iNet), especifica una imagen para cada target usando el formato `target=path`:

```bash
# Flashear los 3 routers y ejecutar un test específico
pytest tests/test_mesh_connectivity.py::test_mesh_basic_connectivity -v -s --log-cli-level=INFO \
  --lg-env targets/mesh_testbed.yaml \
  --firmware belkin_rt3200_1=/home/franco/pi/images/belkin/lime-mediatek-mt7622-linksys_e8450-ubi-squashfs-sysupgrade.itb \
  --firmware belkin_rt3200_2=/home/franco/pi/images/belkin/lime-mediatek-mt7622-linksys_e8450-ubi-squashfs-sysupgrade.itb \
  --firmware gl_mt300n_v2=/home/franco/pi/images/glinet/lime-ramips-mt76x8-glinet_gl-mt300n-v2-squashfs-sysupgrade.bin \
  --flash-firmware
```

**Nota**: Los dispositivos se flashean **secuencialmente** (uno después del otro). El flasheo completo de los 3 routers toma aproximadamente 20-25 minutos.

#### Opción B: Con Makefile - Target `mesh_testbed` (Simplificado)

El Makefile incluye un target preconfigurado para pruebas mesh:

```bash
# Flashear los 3 routers y ejecutar todos los tests mesh
make tests/mesh_testbed \
  FIRMWARE_BELKIN1=/home/franco/pi/images/belkin/lime-mediatek-mt7622-linksys_e8450-ubi-squashfs-sysupgrade.itb \
  FIRMWARE_BELKIN2=/home/franco/pi/images/belkin/lime-mediatek-mt7622-linksys_e8450-ubi-squashfs-sysupgrade.itb \
  FIRMWARE_GLINET=/home/franco/pi/images/glinet/lime-ramips-mt76x8-glinet_gl-mt300n-v2-squashfs-sysupgrade.bin \
  FLASH_FIRMWARE=1

# Ejecutar un test específico sin flashear
make tests/mesh_testbed K=test_mesh_batman_connectivity

# Mantener routers encendidos para debugging
KEEP_DUT_ON=1 make tests/mesh_testbed K=test_mesh_basic_connectivity
```

#### Opción C: Imagen Única para Todos los Targets (Dispositivos homogéneos)

Si todos los dispositivos son del **mismo modelo** (ej. solo Belkin RT3200), puedes usar una sola imagen:

```bash
# Pytest: Flasheo secuencial con imagen única
pytest tests/test_mesh_connectivity.py -v -s --log-cli-level=INFO \
  --lg-env targets/mesh_testbed.yaml \
  --firmware /home/franco/pi/images/belkin/lime-mediatek-mt7622-linksys_e8450-ubi-squashfs-sysupgrade.itb \
  --flash-firmware
```

### 7.3. Características del Sistema de Flasheo Multi-Target

*   **Mapeo Target → Firmware**: Usa `--firmware target=path` para especificar imágenes específicas por dispositivo.
*   **Flasheo Secuencial**: Los dispositivos se flashean uno tras otro (robusto y estable).
*   **Validación**: Todas las imágenes se validan antes de iniciar el flasheo (checksums, compatibilidad de board, espacio en `/tmp`).
*   **Manejo de Errores**: Si algún dispositivo falla, se reportan todos los errores al final.
*   **Configuración Automática**: Después del flasheo, la red se configura automáticamente en cada dispositivo para permitir acceso SSH.
*   **Limpieza de Buffer Serial**: Previene fallos esporádicos causados por mensajes del kernel.

---

## 8. Desarrollos Futuros (Próximos Pasos)

*   **Artefactos y Trazabilidad en CI**: Generar logs y archivos de estado del dispositivo (`serial.log`, `sysupgrade.log`, `uboot.log`, `openwrt_release`, `board.json`, `dmesg`, `sha256.txt`) como artefactos de CI para facilitar la depuración y auditoría.
*   **Selección de Método de Flasheo**: Un flag `--flash-method=auto|sysupgrade|uboot` para controlar explícitamente el método de flasheo.
*   **Tests Mesh Avanzados**: Pruebas de rendimiento de throughput, latencia, y failover en la red mesh con los 3 routers.
*   **Soporte para Más Dispositivos**: Expandir la testbed con dispositivos adicionales para pruebas de escalabilidad mesh.

---
