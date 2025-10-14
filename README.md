# Hardware-in-the-Loop Testing Framework

Framework de testing automatizado para dispositivos OpenWrt/LibreMesh usando Labgrid.

## Documentación

- **Este documento**: Guía de configuración del entorno de testing
- **[instructivo.md](instructivo.md)**: Guía completa del framework, estrategias y drivers

## Introducción

El framework utiliza un sistema de configuración basado en templates YAML y variables de entorno para garantizar la portabilidad entre diferentes máquinas:

- **Templates** (`targets/templates/*.yaml.template`): Archivos de configuración base con variables
- **YAMLs generados** (`targets/*.yaml`): Archivos de configuración específicos generados localmente
- **Auto-detección**: Sistema de detección automática de rutas en el sistema

## Requisitos previos

- Acceso físico a los routers de la testbed
- Arduino con relay control conectado al host
- Repositorio `pi-hil-testing-utils` instalado
- Directorio de imágenes de firmware configurado
- Python 3.8+ y pytest/labgrid instalados

## Configuración inicial

### Configuración automática (recomendado)

1. Clonar el repositorio
2. Ejecutar el script de configuración:
   ```
   cd openwrt-23.05.5/tests/
   ./setup_environment.sh
   ```
3. Verificar que los archivos YAML se hayan generado correctamente

### Configuración manual (estructura personalizada)

1. Copiar el template de configuración:
   ```
   cp tests/env.example tests/.env
   ```
   
2. Editar las variables de entorno según la infraestructura local:
   - Rutas: HIL_UTILS_PATH, HIL_IMAGES_PATH, HIL_TFTP_ROOT
   - Dispositivos: IP, puertos seriales, canales de relay
   - Arduino: Dispositivo, baudrate
   - TFTP: IP del servidor

3. Generar las configuraciones:
   ```
   python3 tests/tools/generate_configs.py
   ```

## Variables de entorno principales

- **HIL_WORKSPACE_PATH**: Ruta al workspace de OpenWrt
- **HIL_UTILS_PATH**: Ruta a pi-hil-testing-utils
- **HIL_IMAGES_PATH**: Directorio de imágenes de firmware
- **HIL_TFTP_ROOT**: Raíz del servidor TFTP
- **HIL_BELKIN1_IP**, **HIL_BELKIN2_IP**, **HIL_GLINET_IP**: IPs de los routers
- **HIL_BELKIN1_SERIAL**, **HIL_BELKIN2_SERIAL**, **HIL_GLINET_SERIAL**: Puertos seriales

## Herramientas disponibles

### setup_environment.sh

Script principal de configuración del entorno con los siguientes modos:
- Configuración completa: `./setup_environment.sh`
- Verificación de configuración: `./setup_environment.sh --check`
- Limpieza de archivos generados: `./setup_environment.sh --clean`

### generate_configs.py

Generador de configuraciones YAML desde templates con expansión de variables.

### validate_setup.sh

Suite de validación automatizada que verifica:
- Funcionalidad de limpieza
- Auto-detección de rutas
- Generación de YAMLs
- Expansión de variables
- Validez de paths

## Solución de problemas

### Paths no encontrados

**Causa**: El script no encuentra las rutas necesarias.
**Solución**: Configurar manualmente las variables de entorno o ajustar la estructura de directorios.

### Variables no expandidas

**Causa**: Las variables no se expandieron correctamente en los YAMLs.
**Solución**: Verificar que las variables estén definidas y regenerar las configuraciones.

### Error de autenticación en GL-iNet

**Causa**: Los YAMLs contienen password pero los dispositivos flasheados no la requieren.
**Solución**: Regenerar configuraciones sin password.

## Flujo de trabajo recomendado

1. Configurar el entorno mediante el script de configuración
2. Verificar la configuración generada
3. Ejecutar los tests usando la estructura de Makefile:
   ```
   make tests/target K=test_name KEEP_DUT_ON=1
   ```
4. Para tests multi-dispositivo, utilizar la configuración mesh_testbed

## Estructura de archivos

```
tests/
├── README.md                       # Este documento
├── instructivo.md                  # Guía del framework
├── setup_environment.sh            # Script principal
├── env.example                     # Template de configuración
├── envrc.template                  # Template para direnv
├── targets/
│   ├── templates/                  # Templates YAML con variables
│   └── *.yaml                      # YAMLs generados (local)
└── tools/
    ├── generate_configs.py         # Generador de configuraciones
    └── validate_setup.sh           # Validador automatizado
```

## Lista de verificación

- Ejecutar `./setup_environment.sh --check` para validar la configuración
- Verificar la generación de los YAMLs con variables expandidas
- Comprobar conectividad física con los dispositivos
- Validar acceso a puertos seriales
- Verificar funcionamiento del Arduino relay
