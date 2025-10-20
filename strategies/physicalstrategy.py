import enum
from time import sleep, time as _time_mod
import attr
import allure
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
        "power": "ExternalPowerDriver",  # Cuando alguien acceda a self.power, dame ExternalPowerDriver
        "shell": "ShellDriver",
        # "ssh": "SSHDriver",  # opcional
    }

    # Configuración de quirks
    requires_serial_disconnect = attr.ib(default=False)
    boot_wait = attr.ib(default=20)
    connection_timeout = attr.ib(default=60)
    smart_state_detection = attr.ib(default=True)
    fast_check_timeout = attr.ib(default=3)

    status = attr.ib(default=Status.unknown)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        # Serial isolator (si existe)
        try:
            self.serial_isolator = self.target.get_driver("SerialIsolatorDriver")
        except Exception:
            self.serial_isolator = None
        try:  # SerialDriver (para "activar" consola con Enter)
            self.serial = self.target.get_driver("SerialDriver")
        except Exception:
            self.serial = None

    @step()
    def _check_shell_active(self):
        """Verifica si el shell ya está activo sin hacer power cycle."""
        if not self.smart_state_detection:
            return False

        try:
            if not hasattr(self.target, 'get_driver'):
                return False
            try:
                serial_driver = self.target.get_driver("SerialDriver")
            except Exception:
                return False

            try:
                serial_driver.sendline("echo test_active")
                result = serial_driver.expect(["test_active", "login:", "#"], timeout=2)
                # test_active o prompt
                return result[0] in (0, 2)
            except Exception:
                return False

        except Exception:
            return False

    @step()
    def _ensure_shell_ready_fast(self):
        """Verificación rápida de que el shell sigue funcionando correctamente."""
        try:
            result = self.shell.run("uname && echo ready", timeout=5)
            if result[2] == 0 and result[0] and len(result[0]) >= 2:
                return ("Linux" in result[0][0]) and ("ready" in result[0][1])
            return False
        except Exception:
            return False

    @step(args=["state"])
    def transition(self, state, *, step):
        if not isinstance(state, Status):
            state = Status[state]

        if state == Status.unknown:
            raise StrategyError(f"can not transition to {state}")
        elif self.status == state:
            step.skip("nothing to do")
            return

        if state == Status.off:
            self.target.activate(self.power)
            self.power.off()

        elif state == Status.shell:
            # ¿ya está activo?
            if self._check_shell_active():
                step.skip("shell already active and ready")
                self.status = Status.shell
                return

            # Power on / cycle
            self.target.activate(self.power)

            if self.requires_serial_disconnect and self.serial_isolator:
                # Secuencia especial GL-iNet
                self.target.activate(self.serial_isolator)
                self.power.off()
                self.serial_isolator.disconnect()
                sleep(3)
                self.power.on()
                sleep(self.boot_wait)
                self.serial_isolator.connect()
                sleep(8)
            else:
                # Arranque estándar
                self.power.on()
                sleep(self.boot_wait)

            # Activar shell normalmente
            self._wait_for_shell_ready()

        self.status = state

    @step()
    def _poke_console(self):
        """Envía uno/dos 'Enter' por serial para activar la consola si está dormida."""
        if not self.serial:
            return
        try:
            for _ in range(2):
                self.serial.sendline("")  # '\n'
                sleep(0.1)
        except Exception as e:
            allure.attach(str(e), "poke-console-error", allure.attachment_type.TEXT)

    @step()
    def _wait_for_shell_ready(self):
        """Espera activamente a que el shell esté listo con timeout robusto."""
        # 1) Despertar consola en serial
        if self.serial:
            self.target.activate(self.serial)
            for _ in range(6):
                try:
                    self.serial.sendline("")
                except Exception:
                    pass
                sleep(0.2)

        # 2) Activar shell
        self.target.activate(self.shell)

        start_time = _time_mod()
        last_error = None

        self._poke_console()

        while _time_mod() - start_time < self.connection_timeout:
            try:
                result = self.shell.run("echo 'ready'", timeout=5)
                if result[2] == 0 and result[0] and "ready" in result[0][0]:
                    return
            except Exception as e:
                last_error = str(e)

            sleep(2)
            self._poke_console()

        msg = f"Shell no estuvo listo en {self.connection_timeout} segundos"
        if last_error:
            allure.attach(last_error, "shell-readiness-last-error", allure.attachment_type.TEXT)
            msg += f". Último error: {last_error}"
        raise StrategyError(msg)

    @step()
    def force_power_cycle(self):
        """Fuerza un power cycle completo, ignorando optimizaciones."""
        original_smart = self.smart_state_detection
        try:
            self.smart_state_detection = False
            self.status = Status.unknown  # Forzar transición
            self.transition("shell")
        finally:
            self.smart_state_detection = original_smart

    @step()
    def ensure_off(self):
        """Asegura que el dispositivo esté apagado."""
        if self.status != Status.off:
            self.transition("off")
        logger.info(f"Device is now off (status: {self.status})")

    @step()
    def cleanup_and_shutdown(self):
        """Limpieza completa y apagado seguro con verificación inteligente."""
        try:
            if self.status == Status.shell:
                try:
                    ssh = self.target.get_driver("SSHDriver")
                    logger.info("Attempting clean shutdown via SSH")
                    ssh.run("poweroff", timeout=10)

                    # intentar detectar caída de SSH por un breve período
                    max_wait = 15
                    check_interval = 2
                    waited = 0
                    while waited < max_wait:
                        try:
                            result = ssh.run("true", timeout=3)
                            if result[2] != 0:
                                break
                        except Exception:
                            logger.info(f"SSH stopped responding after {waited}s - clean shutdown likely completed")
                            break
                        sleep(check_interval)
                        waited += check_interval

                    if waited >= max_wait:
                        logger.warning("SSH still responding after max wait time, forcing physical shutdown")
                    else:
                        logger.info("Clean shutdown via SSH completed")

                except Exception as e:
                    logger.warning(f"SSH shutdown failed: {e}, using physical power off")
        finally:
            self.ensure_off()
            logger.info("Physical power off completed")
