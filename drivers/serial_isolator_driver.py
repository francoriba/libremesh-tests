import subprocess
import attr
from labgrid import target_factory
from labgrid.driver import Driver
from labgrid.resource import Resource
from labgrid.step import step

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
    """Driver para aislar/reconectar línea serial en routers que lo requieren."""

    bindings = {
        "resource": SerialIsolatorResource,
    }

    @step()
    def disconnect(self):
        """Desconecta la línea serial"""
        cmd = [
            "python3", "/home/franco/pi/pi-hil-testing-utils/scripts/arduino_relay_control.py",
            "--port", self.resource.device,
            "--baudrate", str(self.resource.baudrate),
            "on", str(self.resource.channel)
        ]
        subprocess.run(cmd, check=True)

    @step()
    def connect(self):
        """Reconecta la línea serial"""
        cmd = [
            "python3", "/home/franco/pi/pi-hil-testing-utils/scripts/arduino_relay_control.py",
            "--port", self.resource.device,
            "--baudrate", str(self.resource.baudrate),
            "off", str(self.resource.channel)
        ]
        subprocess.run(cmd, check=True)
