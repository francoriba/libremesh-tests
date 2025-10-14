import os
import subprocess
from pathlib import Path
import attr
from labgrid import target_factory
from labgrid.driver import Driver
from labgrid.resource import Resource
from labgrid.step import step


def _get_hil_utils_path():
    """
    Get path to pi-hil-testing-utils with intelligent fallbacks.
    
    Priority:
        1. HIL_UTILS_PATH environment variable
        2. Auto-detection from workspace structure
        3. Default location: ~/pi/pi-hil-testing-utils
    
    Returns:
        str: Path to pi-hil-testing-utils directory
    """
    # Try environment variable first
    env_path = os.environ.get('HIL_UTILS_PATH')
    if env_path and Path(env_path).exists():
        return env_path
    
    # Try auto-detection from workspace structure
    tests_dir = Path(__file__).parent.parent
    workspace_root = tests_dir.parent
    candidate = workspace_root.parent / 'pi-hil-testing-utils'
    if candidate.exists():
        return str(candidate)
    
    # Fall back to default location
    default = Path.home() / 'pi' / 'pi-hil-testing-utils'
    return str(default)


@target_factory.reg_resource
@attr.s(eq=False)
class SerialIsolatorResource(Resource):
    """Recurso para controlar aislamiento de línea serial mediante relé."""
    device = attr.ib(validator=attr.validators.instance_of(str))
    channel = attr.ib(validator=attr.validators.instance_of(int))
    baudrate = attr.ib(default=115200, validator=attr.validators.instance_of(int))


@target_factory.reg_driver
@attr.s(eq=False)
class SerialIsolatorDriver(Driver):
    """
    Driver para aislar/reconectar línea serial en routers que lo requieren.
    
    Utiliza el script arduino_relay_control.py desde pi-hil-testing-utils.
    La ruta al script se detecta automáticamente o se configura con la
    variable de entorno HIL_UTILS_PATH.
    """

    bindings = {
        "resource": SerialIsolatorResource,
    }

    @step()
    def disconnect(self):
        """Desconecta la línea serial"""
        hil_utils = _get_hil_utils_path()
        script_path = Path(hil_utils) / "scripts" / "arduino_relay_control.py"
        
        cmd = [
            "python3", str(script_path),
            "--port", self.resource.device,
            "--baudrate", str(self.resource.baudrate),
            "on", str(self.resource.channel)
        ]
        subprocess.run(cmd, check=True)

    @step()
    def connect(self):
        """Reconecta la línea serial"""
        hil_utils = _get_hil_utils_path()
        script_path = Path(hil_utils) / "scripts" / "arduino_relay_control.py"
        
        cmd = [
            "python3", str(script_path),
            "--port", self.resource.device,
            "--baudrate", str(self.resource.baudrate),
            "off", str(self.resource.channel)
        ]
        subprocess.run(cmd, check=True)
