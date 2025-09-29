import enum
from time import sleep, time as _time_mod
import attr
import logging as _logging
from labgrid import target_factory
from labgrid.step import step
from labgrid.strategy import Strategy, StrategyError

logger = _logging.getLogger(__name__)

class Status(enum.Enum):
    unknown = 0
    off = 1
    shell = 2

@target_factory.reg_driver
@attr.s(eq=False)
class PhysicalDeviceStrategy(Strategy):
    bindings = {
        "power": "ExternalPowerDriver",
        "shell": "ShellDriver",
    }

    # Configuración
    requires_serial_disconnect = attr.ib(default=False)
    boot_wait = attr.ib(default=20)
    connection_timeout = attr.ib(default=60)
    smart_state_detection = attr.ib(default=True)

    status = attr.ib(default=Status.unknown)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        try:
            self.serial_isolator = self.target.get_driver("SerialIsolatorDriver")
        except Exception:
            self.serial_isolator = None
        # SerialDriver para comandos rápidos
        try:
            self.serial = self.target.get_driver("SerialDriver")
        except Exception:
            self.serial = None

    @step()
    def _check_shell_active(self):
        """Verifica rápidamente si el shell ya está activo."""
        if not self.smart_state_detection:
            return False

        try:
            serial_driver = self.target.get_driver("SerialDriver")
            serial_driver.sendline("echo test_active")
            result = serial_driver.expect(["test_active", "login:", "#"], timeout=2)
            return result[0] in (0, 2)  # test_active o prompt
        except Exception:
            return False

    @step(args=["state"])
    def transition(self, state, *, step):
        if not isinstance(state, Status):
            state = Status[state]

        if state == Status.unknown:
            raise StrategyError(f"Cannot transition to {state}")

        if self.status == state:
            step.skip("Already in desired state")
            # activar el ShellDriver incluso si hacemos skip
            if state == Status.shell:
                self.target.activate(self.shell)
            return

        if state == Status.off:
            self._power_off()
        elif state == Status.shell:
            self._power_on_and_wait_shell(step)
            self.target.activate(self.shell)

        self.status = state

    def _power_off(self):
        """Apaga el dispositivo físicamente."""
        self.target.activate(self.power)
        self.power.off()

    def _power_on_and_wait_shell(self, step):
        """Enciende el dispositivo y espera el shell."""
        # Verificar si ya está activo
        if self._check_shell_active():
            step.skip("Shell already active")
            return

        # Power cycle (con quirk GL-iNet si es necesario)
        self.target.activate(self.power)

        # ✅ MANTENER: Quirk especial para GL-iNet
        if self.requires_serial_disconnect and self.serial_isolator:
            logger.info("Using GL-iNet serial isolator sequence")
            self.target.activate(self.serial_isolator)
            self.power.off()
            self.serial_isolator.disconnect()
            sleep(3)
            self.power.on()
            sleep(self.boot_wait)
            self.serial_isolator.connect()
            sleep(8)
        else:
            # Arranque estándar para Belkin
            logger.info("Using standard power-on sequence")
            self.power.on()
            sleep(self.boot_wait)

        # Esperar shell
        self._wait_for_shell()

    @step()
    def _wait_for_shell(self):
        """Espera a que el shell esté listo."""
        # Despertar consola si es necesario
        if self.serial:
            self.target.activate(self.serial)
            for _ in range(3):
                try:
                    self.serial.sendline("")  # Enviar Enter
                except Exception:
                    pass
                sleep(0.2)

        # Activar shell y esperar
        self.target.activate(self.shell)

        start_time = _time_mod()
        while _time_mod() - start_time < self.connection_timeout:
            try:
                result = self.shell.run("echo ready", timeout=5)
                if result[2] == 0 and result[0] and "ready" in result[0][0]:
                    return  # ¡Shell listo!
            except Exception:
                pass  # Continuar intentando

            sleep(2)

        raise StrategyError(f"Shell not ready after {self.connection_timeout}s")

    @step()
    def force_power_cycle(self):
        """Fuerza un power cycle completo, ignorando smart detection."""
        original_smart = self.smart_state_detection
        try:
            self.smart_state_detection = False
            self.status = Status.unknown
            self.transition("shell")
        finally:
            self.smart_state_detection = original_smart

    @step()
    def ensure_off(self):
        """Asegura que el dispositivo esté apagado."""
        if self.status != Status.off:
            self.transition("off")

    @step()
    def cleanup_and_shutdown(self):
        """Apagado limpio vía SSH + apagado físico garantizado."""
        try:
            if self.status == Status.shell:
                self._try_ssh_shutdown()
        finally:
            self.ensure_off()
            logger.info("Physical power off completed")

    def _try_ssh_shutdown(self):
        """Intenta apagado limpio vía SSH."""
        try:
            ssh = self.target.get_driver("SSHDriver")
            logger.info("Attempting clean shutdown via SSH")
            ssh.run("poweroff", timeout=10)

            # Esperar que SSH deje de responder (máximo 15s)
            for i in range(8):  # 8 * 2s = 16s máximo
                try:
                    ssh.run("true", timeout=3)
                    sleep(2)
                except Exception:
                    logger.info(f"SSH stopped responding after {(i + 1) * 2}s - clean shutdown completed")
                    return

            logger.warning("SSH still responding after 16s, will force physical shutdown")

        except Exception as e:
            logger.warning(f"SSH shutdown failed: {e}, using physical power off")

