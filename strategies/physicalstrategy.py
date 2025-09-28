import enum
from time import sleep, time
import attr
import allure
from labgrid import target_factory
from labgrid.step import step
from labgrid.strategy import Strategy, StrategyError

class Status(enum.Enum):
    unknown = 0
    off = 1
    shell = 2

@target_factory.reg_driver
@attr.s(eq=False)
class PhysicalDeviceStrategy(Strategy):
    bindings = {
        "power": "ExternalPowerDriver", # Esto le dice a Labgrid: "Cuando alguien acceda a self.power, dame una instancia del ExternalPowerDriver"
        "shell": "ShellDriver",
        # "ssh": "SSHDriver",  # opcional: comentar si no se usa
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
        try: # SerialDriver (para "activar" consola con Enter)
            self.serial = self.target.get_driver("SerialDriver")
        except Exception:
            self.serial = None

    @step()
    def _check_shell_active(self):
        """Verifica si el shell ya está activo sin hacer power cycle."""
        if not self.smart_state_detection:
            return False

        try:
            # VERIFICACIÓN RÁPIDA SIN ACTIVAR SHELL
            # Solo verificamos si el SerialDriver puede leer algo inmediatamente
            if not hasattr(self.target, 'get_driver'):
                return False
            try:
                serial_driver = self.target.get_driver("SerialDriver")
            except:
                return False

            # Verificación súper rápida: enviar comando y leer con timeout mínimo
            try:
                serial_driver.sendline("echo test_active")
                # Timeout de solo 2 segundos
                result = serial_driver.expect(["test_active", "login:", "#"], timeout=2)

                # Si recibimos cualquier respuesta válida, el router está activo
                if result[0] in [0, 2]:  # test_active o prompt
                    return True
                else:
                    return False

            except Exception:
                # Si hay timeout o error, router está apagado/no responde
                return False

        except Exception:
            return False

    @step()
    def _ensure_shell_ready_fast(self):
        """Verificación rápida de que el shell sigue funcionando correctamente."""
        try:
            # Comando de verificación más completo
            result = self.shell.run("uname && echo ready", timeout=5)
            if result[2] == 0 and result[0] and len(result[0]) >= 2:
                if "Linux" in result[0][0] and "ready" in result[0][1]:
                    return True
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
            # Verificación simple: ¿ya está activo?
            if self._check_shell_active():
                step.skip("shell already active and ready")
                self.status = Status.shell
                return

            # Si llegamos aquí, hacer power cycle completo
            self.target.activate(self.power)

            if self.requires_serial_disconnect: #and self.serial_isolator:
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
            # En algunos getty hace falta apretar Enter más de una vez
            for _ in range(2):
                self.serial.sendline("")  # equivale a '\n'
                sleep(0.1)
        except Exception as e:
            allure.attach(str(e), "poke-console-error", allure.attachment_type.TEXT)

    @step()
    def _wait_for_shell_ready(self):
        """Espera activamente a que el shell esté listo con timeout robusto."""

        # 1) ACTIVAR SERIAL Y DESPERTAR CONSOLA ANTES DE SHELL
        if self.serial:
            self.target.activate(self.serial)
            # Enviar varios Enter para salir de "Please press Enter to activate this console."
            for _ in range(6):
                try:
                    self.serial.sendline("")
                except Exception:
                    pass
                sleep(0.2)

        # 2) Ahora sí, activar el shell (ya no debería bloquearse)
        self.target.activate(self.shell)

        start_time = time()
        last_error = None

        # Primer poke adicional por si acaso
        self._poke_console()

        while time() - start_time < self.connection_timeout:
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
