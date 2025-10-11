import enum
import os
import shutil
from time import sleep, time as _time_mod
import attr
import logging as _logging
import shlex
from labgrid import target_factory
from labgrid.step import step
from labgrid.strategy import Strategy, StrategyError

logger = _logging.getLogger(__name__)


class Status(enum.Enum):
    unknown = 0
    off = 1
    shell = 2


# Timeouts and delays (in seconds)
class Timeouts:
    SHELL_CHECK = 2
    SHELL_READY = 5
    SHELL_RETRY_INTERVAL = 2
    POWER_OFF_DELAY = 3
    SERIAL_RECONNECT = 8
    SHUTDOWN_COMMAND = 10
    SHUTDOWN_CHECK = 2
    CONNECTION_CHECK = 3
    BUFFER_FLUSH = 0.5
    FIRMWARE_DETECTION = 5
    COMMAND_EXECUTION = 10
    NETWORK_RESTART_OPENWRT = 10
    NETWORK_RESTART_LIBREMESH = 30
    CONSOLE_VERIFICATION = 5
    REBOOT_COMMAND_DELAY = 2
    REBOOT_MINIMUM_OPENWRT = 30
    REBOOT_MINIMUM_LIBREMESH = 90
    NETWORK_INIT_LIBREMESH = 60
    NETWORK_INIT_OPENWRT = 15
    TFTP_DOWNLOAD = 60
    REBOOT_START_DELAY = 10


class Indices:
    STDOUT = 0
    EXIT_CODE = 2
    TEST_ACTIVE_MATCH = 0
    PROMPT_MATCH = 2
    SERIAL_BEFORE_CONTENT = 1


class Permissions:
    TFTP_FILE = 0o644


class ShutdownConfig:
    MAX_WAIT_CYCLES = 8


class WakeConsoleConfig:
    ATTEMPTS = 3
    DELAY = 0.2


@target_factory.reg_driver
@attr.s(eq=False)
class PhysicalDeviceStrategy(Strategy):
    """Strategy for managing physical device power, boot, and recovery."""

    bindings = {
        "power": "ExternalPowerDriver",
        "shell": "ShellDriver",
    }

    # Boot configuration
    requires_serial_disconnect = attr.ib(default=False)
    boot_wait = attr.ib(default=20)
    connection_timeout = attr.ib(default=60)
    smart_state_detection = attr.ib(default=True)

    # Recovery configuration
    enable_uboot_recovery = attr.ib(default=False)
    max_recovery_attempts = attr.ib(default=2)
    tftp_root = attr.ib(default="/srv/tftp")

    # U-Boot recovery parameters
    uboot_interrupt_delay = attr.ib(default=0.2, validator=attr.validators.instance_of((int, float)))
    uboot_interrupt_count = attr.ib(default=15, validator=attr.validators.instance_of(int))
    uboot_power_off_wait = attr.ib(default=5, validator=attr.validators.instance_of(int))
    uboot_boot_wait = attr.ib(default=120, validator=attr.validators.instance_of(int))

    # Network configuration parameters
    post_dhcp_wait = attr.ib(default=15, validator=attr.validators.instance_of(int))
    network_config_retry_wait = attr.ib(default=1, validator=attr.validators.instance_of(int))

    status = attr.ib(default=Status.unknown)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self.serial_isolator = self._get_optional_driver("SerialIsolatorDriver")
        self.serial = self._get_optional_driver("SerialDriver")

    def _get_optional_driver(self, driver_name):
        """Safely retrieves an optional driver, returns None if not available."""
        try:
            return self.target.get_driver(driver_name)
        except Exception:
            return None

    @step()
    def _check_shell_active(self):
        """Quickly checks if the shell is already active."""
        if not self.smart_state_detection:
            return False

        try:
            serial_driver = self.target.get_driver("SerialDriver")
            serial_driver.sendline("echo test_active")

            result = serial_driver.expect(
                ["test_active", "login:", "#"],
                timeout=Timeouts.SHELL_CHECK
            )

            return result[0] in (Indices.TEST_ACTIVE_MATCH, Indices.PROMPT_MATCH)
        except Exception:
            return False

    @step(args=["state"])
    def transition(self, state, *, step):
        """Transitions device to the specified state."""
        if not isinstance(state, Status):
            state = Status[state]

        if state == Status.unknown:
            raise StrategyError(f"Cannot transition to {state}")

        if self.status == state:
            step.skip("Already in desired state")
            if state == Status.shell:
                self.target.activate(self.shell)
            return

        if state == Status.off:
            self._power_off()
        elif state == Status.shell:
            self._power_on_and_wait_for_shell(step)
            self.target.activate(self.shell)

        self.status = state

    def _power_off(self):
        """Powers off the device physically."""
        self.target.activate(self.power)
        self.power.off()

    def _power_on_and_wait_for_shell(self, step):
        """Powers on the device and waits for shell to become available."""
        if self._check_shell_active():
            step.skip("Shell already active")
            return

        self.target.activate(self.power)

        if self.requires_serial_disconnect and self.serial_isolator:
            self._power_cycle_with_serial_isolation()
        else:
            self._perform_standard_power_on()

        self._wait_for_shell()

    def _power_cycle_with_serial_isolation(self):
        """Power cycles device with serial isolation (GL-iNet specific)."""
        logger.info("Using GL-iNet serial isolator sequence")
        self.target.activate(self.serial_isolator)

        self.power.off()
        self.serial_isolator.disconnect()
        sleep(Timeouts.POWER_OFF_DELAY)

        self.power.on()
        sleep(self.boot_wait)

        self.serial_isolator.connect()
        sleep(Timeouts.SERIAL_RECONNECT)

    def _perform_standard_power_on(self):
        """Standard power-on sequence (Belkin and most devices)."""
        logger.info("Using standard power-on sequence")
        self.power.on()
        sleep(self.boot_wait)

    @step()
    def _wait_for_shell(self):
        """Waits for the shell to be ready."""
        self._wake_console()
        self.target.activate(self.shell)

        start_time = _time_mod()

        while _time_mod() - start_time < self.connection_timeout:
            if self._is_shell_ready():
                return

            sleep(Timeouts.SHELL_RETRY_INTERVAL)

        raise StrategyError(f"Shell not ready after {self.connection_timeout}s")

    def _is_shell_ready(self):
        """Checks if shell is responding correctly."""
        try:
            result = self.shell.run("echo ready", timeout=Timeouts.SHELL_READY)
            return (result[Indices.EXIT_CODE] == 0 and
                    result[Indices.STDOUT] and
                    "ready" in result[Indices.STDOUT][0])
        except Exception:
            return False

    def _wake_console(self):
        """Sends newlines to wake up the console."""
        if not self.serial:
            return

        self.target.activate(self.serial)

        for _ in range(WakeConsoleConfig.ATTEMPTS):
            try:
                self.serial.sendline("")
            except Exception:
                pass
            sleep(WakeConsoleConfig.DELAY)

    @step()
    def force_power_cycle(self):
        """Forces a complete power cycle, ignoring smart detection."""
        original_smart_detection = self.smart_state_detection
        try:
            self.smart_state_detection = False
            self.status = Status.unknown
            self.transition("shell")
        finally:
            self.smart_state_detection = original_smart_detection

    @step()
    def ensure_off(self):
        """Ensures the device is powered off."""
        if self.status != Status.off:
            self.transition("off")

    @step()
    def cleanup_and_shutdown(self):
        """Performs clean shutdown via SSH followed by guaranteed physical power off."""
        try:
            if self.status == Status.shell:
                self._attempt_graceful_ssh_shutdown()
        finally:
            self.ensure_off()
            logger.info("Physical power off completed")

    def _attempt_graceful_ssh_shutdown(self):
        """Attempts clean shutdown via SSH."""
        try:
            ssh = self.target.get_driver("SSHDriver")
            logger.info("Attempting clean shutdown via SSH")

            ssh.run("poweroff", timeout=Timeouts.SHUTDOWN_COMMAND)

            for cycle in range(ShutdownConfig.MAX_WAIT_CYCLES):
                if self._is_ssh_connection_dead(ssh, cycle):
                    return

            self._log_shutdown_timeout_warning()

        except Exception as e:
            logger.warning(f"SSH shutdown failed: {e}, using physical power off")

    def _is_ssh_connection_dead(self, ssh, cycle_number):
        """Checks if SSH connection has terminated (indicating successful shutdown)."""
        try:
            ssh.run("true", timeout=Timeouts.CONNECTION_CHECK)
            sleep(Timeouts.SHUTDOWN_CHECK)
            return False
        except Exception:
            elapsed_time = (cycle_number + 1) * Timeouts.SHUTDOWN_CHECK
            logger.info(f"SSH stopped responding after {elapsed_time}s - clean shutdown completed")
            return True

    def _log_shutdown_timeout_warning(self):
        """Logs warning when SSH shutdown takes too long."""
        max_wait_time = ShutdownConfig.MAX_WAIT_CYCLES * Timeouts.SHUTDOWN_CHECK
        logger.warning(f"SSH still responding after {max_wait_time}s, will force physical shutdown")

    @step()
    def configure_libremesh_network(self, reboot_after: bool = False):
        """
        Configures LibreMesh network to enable SSH access from test infrastructure.

        LibreMesh defaults to static IP configuration. This method configures
        the LAN interface to use DHCP, allowing the device to obtain an IP
        address from the test network.

        Tries SSH first (preferred), falls back to serial console if unavailable.

        Args:
            reboot_after: If True, reboots device after configuration for persistence
        """
        logger.info("Configuring LibreMesh network interface for testbed access")

        if self._try_configure_network_via_ssh():
            return

        self._configure_network_via_serial(reboot_after=reboot_after)

    def _try_configure_network_via_ssh(self):
        """
        Attempts to configure network via SSH.

        Returns:
            bool: True if successful, False if SSH unavailable
        """
        try:
            ssh = self.target.get_driver("SSHDriver", activate=False)
            self.target.activate(ssh)

            logger.info("Using SSH for network configuration")
            self._execute_network_config_commands_via_ssh(ssh)
            self._wait_for_network_reconfiguration()

            logger.info("Network configuration via SSH completed")
            return True

        except Exception as e:
            logger.info(f"SSH not available for network config, falling back to serial: {e}")
            return False

    def _execute_network_config_commands_via_ssh(self, ssh):
        """Executes network configuration commands via SSH."""
        network_config_commands = [
            "uci set network.lan.proto='dhcp'",
            "uci commit network",
            "/etc/init.d/network restart",
        ]

        for cmd in network_config_commands:
            try:
                result = ssh.run(cmd, timeout=Timeouts.COMMAND_EXECUTION)
                logger.debug(f"Network config command (SSH) '{cmd}' exited with code {result[Indices.EXIT_CODE]}")
            except Exception as e:
                logger.warning(f"Network config command (SSH) '{cmd}' failed: {e}")

    def _wait_for_network_reconfiguration(self):
        """Waits for network to reconfigure after applying changes."""
        logger.info(f"Waiting {self.post_dhcp_wait}s for network reconfiguration")
        sleep(self.post_dhcp_wait)

    def _configure_network_via_serial(self, reboot_after: bool = False):
        """
        Configures network via serial console (fallback method).

        Args:
            reboot_after: If True, reboots device after configuration for persistence
        """
        self.target.activate(self.shell)
        self._flush_serial_buffer()

        is_libremesh = self._detect_firmware_type()
        network_config_commands = self._get_network_config_commands(is_libremesh, reboot_after)
        network_restart_timeout = self._get_network_restart_timeout(is_libremesh)

        self._execute_network_config_commands_via_serial(
            network_config_commands,
            network_restart_timeout
        )

        if reboot_after:
            self._reboot_and_wait_for_shell(is_libremesh)
        else:
            self._wait_for_network_initialization(is_libremesh)

        logger.info("Network configuration via serial completed")

    def _flush_serial_buffer(self):
        """Flushes serial buffer before network configuration."""
        try:
            serial = self.target.get_driver("SerialDriver")
            logger.debug("Flushing serial buffer before network config")
            serial.sendline("")
            sleep(Timeouts.BUFFER_FLUSH)
        except Exception as e:
            logger.warning(f"Could not flush serial buffer: {e}")

    def _detect_firmware_type(self):
        """
        Detects if device is running LibreMesh or vanilla OpenWrt.

        Returns:
            bool: True if LibreMesh, False if vanilla OpenWrt
        """
        try:
            serial = self.target.get_driver("SerialDriver")
            logger.debug("Detecting firmware type (LibreMesh vs OpenWrt)")

            if self._check_libremesh_hostname(serial):
                logger.info("Detected LibreMesh firmware (hostname check)")
                return True

            if self._check_libremesh_package(serial):
                logger.info("Detected LibreMesh firmware (package check)")
                return True

            logger.info("Detected vanilla OpenWrt firmware")
            return False

        except Exception as e:
            logger.warning(f"Could not detect firmware type, assuming LibreMesh: {e}")
            return True

    def _check_libremesh_hostname(self, serial):
        """Checks if hostname indicates LibreMesh firmware."""
        serial.sendline("uname -n")
        sleep(Timeouts.BUFFER_FLUSH)
        result = serial.expect('#', timeout=Timeouts.FIRMWARE_DETECTION)
        response = result[Indices.SERIAL_BEFORE_CONTENT] if isinstance(result, tuple) else result
        return isinstance(response, bytes) and b'LiMe-' in response

    def _check_libremesh_package(self, serial):
        """Checks if LibreMesh package is installed."""
        serial.sendline("opkg list-installed | grep -q '^lime-system' && echo LIME || echo OPENWRT")
        sleep(Timeouts.BUFFER_FLUSH)
        result = serial.expect('#', timeout=Timeouts.FIRMWARE_DETECTION)
        response = result[Indices.SERIAL_BEFORE_CONTENT] if isinstance(result, tuple) else result
        return isinstance(response, bytes) and b'LIME' in response

    def _get_network_config_commands(self, is_libremesh, reboot_after):
        """
        Gets appropriate network configuration commands based on firmware type.

        Args:
            is_libremesh: True if LibreMesh firmware, False if OpenWrt
            reboot_after: True if device will be rebooted after configuration

        Returns:
            list: Network configuration commands
        """
        if is_libremesh:
            return self._get_libremesh_network_commands(reboot_after)
        return self._get_openwrt_network_commands(reboot_after)

    def _get_libremesh_network_commands(self, reboot_after):
        """Gets LibreMesh-specific network configuration commands."""
        logger.debug("Using LibreMesh network configuration commands")

        commands = [
            'uci set lime-defaults.lan.proto=dhcp 2>/dev/null; true',
            'uci -q delete lime-defaults.lan.ipaddr 2>/dev/null; true',
            'uci -q delete lime-defaults.lan.netmask 2>/dev/null; true',
            'uci commit lime-defaults 2>/dev/null; true',
            'uci set lime.lan.proto=dhcp 2>/dev/null; true',
            'uci -q delete lime.lan.ipaddr 2>/dev/null; true',
            'uci -q delete lime.lan.netmask 2>/dev/null; true',
            'uci commit lime 2>/dev/null; true',
            'uci set network.lan.proto=dhcp',
            'uci -q delete network.lan.ipaddr',
            'uci -q delete network.lan.netmask',
            'uci commit network',
            'sync',
        ]

        if not reboot_after:
            commands.append('/etc/init.d/network restart')

        return commands

    def _get_openwrt_network_commands(self, reboot_after):
        """Gets vanilla OpenWrt network configuration commands."""
        logger.debug("Using vanilla OpenWrt network configuration commands")

        commands = [
            'uci set network.lan.proto=dhcp',
            'uci -q delete network.lan.ipaddr',
            'uci -q delete network.lan.netmask',
            'uci commit network',
            'sync',
        ]

        if not reboot_after:
            commands.append('/etc/init.d/network restart')

        return commands

    def _get_network_restart_timeout(self, is_libremesh):
        """Gets appropriate timeout for network restart based on firmware type."""
        return Timeouts.NETWORK_RESTART_LIBREMESH if is_libremesh else Timeouts.NETWORK_RESTART_OPENWRT

    def _execute_network_config_commands_via_serial(self, commands, network_restart_timeout):
        """Executes network configuration commands via serial console."""
        serial = self.target.get_driver("SerialDriver")

        for cmd in commands:
            self._execute_single_network_command(serial, cmd, network_restart_timeout)

    def _execute_single_network_command(self, serial, command, network_restart_timeout):
        """Executes a single network configuration command."""
        try:
            logger.debug(f"Sending network config command: {command}")
            serial.sendline(command)
            sleep(self.network_config_retry_wait)

            timeout = network_restart_timeout if 'network restart' in command else Timeouts.COMMAND_EXECUTION

            if 'network restart' in command:
                logger.debug(f"Waiting up to {timeout}s for network restart to complete")

            try:
                serial.expect('#', timeout=timeout)
                logger.debug(f"Network config command '{command}' sent successfully")
            except Exception as e:
                logger.debug(f"Expect after '{command}' had issues (non-fatal): {e}")

        except Exception as e:
            logger.warning(f"Network config command '{command}' failed: {e}")

    def _reboot_and_wait_for_shell(self, is_libremesh):
        """Reboots device and waits for shell to become available."""
        self._verify_console_responsive_before_reboot()
        self._send_reboot_command()

        reboot_wait = self._calculate_reboot_wait_time(is_libremesh)
        logger.info(f"Waiting {reboot_wait}s for device reboot and network initialization")
        sleep(reboot_wait)

        self._wait_for_shell()
        logger.info("Network configuration via serial completed (device rebooted)")

    def _verify_console_responsive_before_reboot(self):
        """Verifies serial console is responsive before sending reboot command."""
        serial = self.target.get_driver("SerialDriver")
        logger.debug("Verifying serial console is responsive before reboot")

        try:
            serial.sendline('echo "ready_to_reboot"')
            serial.expect('ready_to_reboot', timeout=Timeouts.CONSOLE_VERIFICATION)
            serial.expect('#', timeout=Timeouts.CONSOLE_VERIFICATION)
            logger.debug("Console confirmed responsive, proceeding with reboot")
        except Exception as e:
            logger.warning(f"Console verification before reboot had issues: {e}")

    def _send_reboot_command(self):
        """Sends reboot command and deactivates shell."""
        logger.info("Rebooting device to apply persistent network configuration")

        try:
            self.target.deactivate(self.shell)
        except Exception as e:
            logger.debug(f"Shell deactivation before reboot had issues (non-fatal): {e}")

        try:
            serial = self.target.get_driver("SerialDriver")
            serial.sendline('reboot')
            sleep(Timeouts.REBOOT_COMMAND_DELAY)
        except Exception as e:
            logger.warning(f"Reboot command had issues: {e}")

    def _calculate_reboot_wait_time(self, is_libremesh):
        """Calculates appropriate wait time for reboot based on firmware type."""
        if is_libremesh:
            return max(self.post_dhcp_wait, Timeouts.REBOOT_MINIMUM_LIBREMESH)
        return max(self.post_dhcp_wait, Timeouts.REBOOT_MINIMUM_OPENWRT)

    def _wait_for_network_initialization(self, is_libremesh):
        """Waits for network to initialize after configuration without reboot."""
        if is_libremesh:
            wait_time = max(self.post_dhcp_wait, Timeouts.NETWORK_INIT_LIBREMESH)
            logger.info(f"Waiting {wait_time}s for LibreMesh mesh network initialization (no reboot)")
        else:
            wait_time = max(self.post_dhcp_wait, Timeouts.NETWORK_INIT_OPENWRT)
            logger.info(f"Waiting {wait_time}s for network reconfiguration (no reboot)")

        sleep(wait_time)

    @step()
    def attempt_uboot_recovery(self, firmware_image: str):
        """
        Attempts to recover device using U-Boot and TFTP.

        Recovery process:
        1. Power cycles the device
        2. Interrupts U-Boot bootloader
        3. Loads initramfs via TFTP to RAM
        4. Boots temporarily from RAM
        5. Uploads and persists sysupgrade image to flash

        Args:
            firmware_image: Path to sysupgrade image for final persistence

        Raises:
            StrategyError: If U-Boot recovery fails at any step
        """
        logger.warning("Attempting U-Boot recovery via TFTP + initramfs")

        uboot = self._get_uboot_driver()
        serial = self._get_serial_driver()

        initramfs_filename = self._extract_initramfs_filename(uboot, firmware_image)
        self._prepare_tftp_files(firmware_image, initramfs_filename)

        self._prepare_device_for_uboot()
        self._boot_initramfs_from_uboot(uboot, serial)
        self._persist_sysupgrade_firmware(firmware_image, uboot)

        logger.info("U-Boot recovery with initramfs→sysupgrade persistence completed successfully")

    def _get_uboot_driver(self):
        """Retrieves U-Boot driver without activating it."""
        try:
            return self.target.get_driver("UBootDriver", activate=False)
        except Exception as e:
            raise StrategyError(f"U-Boot recovery not configured: {e}")

    def _get_serial_driver(self):
        """Retrieves Serial driver without activating it."""
        try:
            return self.target.get_driver("SerialDriver", activate=False)
        except Exception as e:
            raise StrategyError(f"SerialDriver not available: {e}")

    def _extract_initramfs_filename(self, uboot, firmware_image):
        """Extracts initramfs filename from U-Boot configuration."""
        if hasattr(uboot, 'init_commands') and uboot.init_commands:
            for cmd in uboot.init_commands:
                if 'setenv bootfile' in cmd:
                    return cmd.split()[-1]

        logger.warning("No initramfs bootfile found in U-Boot config, using sysupgrade image")
        return os.path.basename(firmware_image)

    def _prepare_tftp_files(self, firmware_image, initramfs_filename):
        """Copies required firmware files to TFTP root directory."""
        initramfs_path = os.path.join(os.path.dirname(firmware_image), initramfs_filename)
        tftp_initramfs_path = os.path.join(self.tftp_root, initramfs_filename)

        if os.path.exists(initramfs_path):
            if self._should_copy_file(initramfs_path, tftp_initramfs_path):
                self._copy_to_tftp(initramfs_path, tftp_initramfs_path, "initramfs")
            else:
                logger.info(f"Initramfs already in TFTP root (up to date): {tftp_initramfs_path}")
        else:
            self._verify_initramfs_exists_in_tftp(tftp_initramfs_path, initramfs_filename)

    def _verify_initramfs_exists_in_tftp(self, tftp_path, filename):
        """Verifies that initramfs file exists in TFTP root."""
        logger.warning(f"Initramfs not found in images directory, checking TFTP root")
        if not os.path.exists(tftp_path):
            raise StrategyError(f"Initramfs not found in images dir or TFTP root: {filename}")

    def _should_copy_file(self, source_path, dest_path):
        """Checks if file needs to be copied based on modification time."""
        if not os.path.exists(dest_path):
            return True

        try:
            return os.path.getmtime(source_path) > os.path.getmtime(dest_path)
        except Exception:
            return True

    def _copy_to_tftp(self, source_path, dest_path, file_description):
        """Copies file to TFTP root with proper permissions."""
        logger.info(f"Copying {file_description} to TFTP root: {dest_path}")

        try:
            self._copy_file_with_permissions(source_path, dest_path)
        except PermissionError:
            self._copy_file_with_sudo(source_path, dest_path, file_description)

    def _copy_file_with_permissions(self, source_path, dest_path):
        """Copies file and sets appropriate permissions."""
        shutil.copy2(source_path, dest_path)
        os.chmod(dest_path, Permissions.TFTP_FILE)

    def _copy_file_with_sudo(self, source_path, dest_path, file_description):
        """Copies file using sudo when permissions are insufficient."""
        logger.info(f"Using sudo to copy {file_description} (permission required)")
        os.system(f"sudo cp {shlex.quote(source_path)} {shlex.quote(dest_path)}")
        os.system(f"sudo chown tftp:tftp {shlex.quote(dest_path)}")
        os.system(f"sudo chmod 644 {shlex.quote(dest_path)}")

    def _prepare_device_for_uboot(self):
        """Ensures device is powered off and drivers are deactivated."""
        logger.info("Ensuring device is powered off")
        self.target.activate(self.power)
        self.power.off()
        sleep(self.uboot_power_off_wait)

        self._deactivate_shell_and_ssh()

    def _deactivate_shell_and_ssh(self):
        """Deactivates shell and SSH drivers to clear state."""
        self._deactivate_driver_safely(self.shell)

        try:
            ssh = self.target.get_driver("SSHDriver", activate=False)
            self._deactivate_driver_safely(ssh)
        except Exception:
            pass

    def _deactivate_driver_safely(self, driver):
        """Safely deactivates a driver without raising exceptions."""
        try:
            if driver in self.target.active:
                self.target.deactivate(driver)
        except Exception:
            pass

    def _boot_initramfs_from_uboot(self, uboot, serial):
        """Boots initramfs from U-Boot via TFTP."""
        self.target.activate(serial)

        logger.info("Powering on device for U-Boot access")
        self.power.on()

        self._interrupt_uboot_bootloader(serial)
        self._load_and_boot_initramfs(uboot)
        self._wait_for_shell_after_uboot_boot()

    def _interrupt_uboot_bootloader(self, serial):
        """Sends interrupt characters to catch U-Boot bootloader."""
        logger.info(f"Sending {self.uboot_interrupt_count} interrupt characters to catch U-Boot")

        for _ in range(self.uboot_interrupt_count):
            sleep(self.uboot_interrupt_delay)
            serial.write(b' ')

        logger.info("Waiting for U-Boot prompt")

    def _load_and_boot_initramfs(self, uboot):
        """Activates U-Boot and boots the loaded initramfs."""
        self.target.activate(uboot)
        logger.info("U-Boot prompt acquired, TFTP download should be in progress")

        logger.info("Booting initramfs from RAM...")
        uboot.boot("")
        uboot.await_boot()

        logger.info("U-Boot recovery: device booted from RAM, waiting for shell...")
        self.target.deactivate(uboot)

    def _wait_for_shell_after_uboot_boot(self):
        """Waits for Linux shell to become available after U-Boot boot."""
        logger.info(f"Waiting up to {self.uboot_boot_wait}s for Linux shell after RAM boot...")
        self.status = Status.unknown

        for _ in range(self.uboot_boot_wait):
            if self._attempt_shell_activation():
                logger.info("Shell access acquired after initramfs RAM boot")
                return
            sleep(1)

        raise StrategyError("Failed to get shell after U-Boot initramfs boot")

    def _attempt_shell_activation(self):
        """Attempts to activate shell, returns True if successful."""
        try:
            if self._check_shell_active():
                self.target.activate(self.shell)
                return True
        except Exception:
            pass
        return False

    def _persist_sysupgrade_firmware(self, firmware_image, uboot):
        """Persists sysupgrade firmware to flash after RAM boot."""
        logger.info("Persisting sysupgrade firmware to flash...")

        firmware_path = self._upload_sysupgrade_firmware(firmware_image, uboot)
        self._execute_sysupgrade(firmware_path)
        self._wait_for_device_reboot_after_sysupgrade()

    def _upload_sysupgrade_firmware(self, firmware_image, uboot):
        """
        Uploads sysupgrade firmware to device.

        Returns:
            str: Path to firmware on device
        """
        try:
            return self._upload_firmware_via_ssh(firmware_image)
        except Exception as e:
            logger.warning(f"SSH not available for upload: {e}")
            return self._download_firmware_via_tftp(firmware_image, uboot)

    def _upload_firmware_via_ssh(self, firmware_image):
        """Uploads firmware via SSH."""
        ssh = self.target.get_driver("SSHDriver", activate=False)
        self.target.activate(ssh)

        logger.info("SSH available, uploading sysupgrade firmware for persistence")
        device_firmware_path = "/tmp/recovery_sysupgrade.bin"
        ssh.put(firmware_image, device_firmware_path)
        return device_firmware_path

    def _download_firmware_via_tftp(self, firmware_image, uboot):
        """Downloads firmware via TFTP (fallback method)."""
        firmware_basename = os.path.basename(firmware_image)
        tftp_sysupgrade_path = os.path.join(self.tftp_root, firmware_basename)

        self._ensure_firmware_in_tftp_root(firmware_image, tftp_sysupgrade_path)

        logger.info("Trying to download sysupgrade via serial from TFTP...")
        serverip = self._extract_tftp_server_ip(uboot)
        logger.info(f"Downloading from TFTP server {serverip}")

        return self._perform_tftp_download(firmware_basename, serverip)

    def _ensure_firmware_in_tftp_root(self, source_path, tftp_path):
        """Ensures firmware file exists in TFTP root directory."""
        if not os.path.exists(tftp_path):
            logger.info(f"Copying sysupgrade to TFTP root: {tftp_path}")
            shutil.copy2(source_path, tftp_path)
            os.system(f"sudo chown tftp:tftp {shlex.quote(tftp_path)}")
            os.system(f"sudo chmod 644 {shlex.quote(tftp_path)}")

    def _perform_tftp_download(self, firmware_basename, serverip):
        """Performs TFTP download and returns device path."""
        try:
            self.shell.run(
                f"cd /tmp && tftp -g -r {firmware_basename} {serverip}",
                timeout=Timeouts.TFTP_DOWNLOAD
            )
            return f"/tmp/{firmware_basename}"
        except Exception as download_error:
            raise StrategyError(f"Failed to upload/download sysupgrade firmware: {download_error}")

    def _extract_tftp_server_ip(self, uboot):
        """Extracts TFTP server IP from U-Boot configuration."""
        for cmd in getattr(uboot, "init_commands", []) or []:
            if cmd.startswith("setenv serverip "):
                return cmd.split()[-1]

        raise StrategyError("serverip not found in U-Boot init_commands")

    def _execute_sysupgrade(self, firmware_path):
        """Executes sysupgrade command to persist firmware."""
        logger.info(f"Running sysupgrade -n -F {firmware_path} to persist firmware...")

        try:
            self.shell.console.sendline(f"sysupgrade -n -F {firmware_path}")
            logger.info("Sysupgrade command sent, device is rebooting...")
        except Exception as e:
            logger.info(f"Shell closed during sysupgrade (expected): {e}")

    def _wait_for_device_reboot_after_sysupgrade(self):
        """Waits for device to reboot after sysupgrade."""
        logger.info("Waiting for device to reboot after persistence...")
        sleep(Timeouts.REBOOT_START_DELAY)

        self._deactivate_shell_and_ssh()
        self.status = Status.unknown

    @step()
    def flash_firmware(self, image_path: str, keep_config: bool = False, ensure_network: bool = True):
        """
        Flashes firmware and waits for device to boot.

        If boot fails and U-Boot recovery is enabled, attempts automatic recovery.

        Args:
            image_path: Path to firmware image file
            keep_config: If True, preserves device configuration
            ensure_network: If True, ensures network is configured before flashing

        Raises:
            StrategyError: If flash and all recovery attempts fail
        """
        if self.status != Status.shell:
            self.transition("shell")

        if ensure_network:
            self._ensure_network_configured_before_flash()

        self._perform_firmware_flash(image_path, keep_config)
        self._attempt_boot_with_recovery(image_path)

    def _ensure_network_configured_before_flash(self):
        """Ensures network is configured for SSH access before attempting flash."""
        logger.info("Ensuring network configuration before firmware upload")

        try:
            ssh = self.target.get_driver("SSHDriver", activate=False)
            self.target.activate(ssh)
            logger.info("SSH already available, skipping network configuration")
        except Exception as e:
            logger.info(f"SSH not available ({e}), configuring network via serial")
            self.configure_libremesh_network(reboot_after=False)

    def _perform_firmware_flash(self, image_path, keep_config):
        """Performs the actual firmware flash operation."""
        sysupgrade = self._get_sysupgrade_driver()
        sysupgrade.flash(image_path, keep_config=keep_config)

        self._deactivate_drivers_after_flash()
        self.status = Status.unknown

    def _get_sysupgrade_driver(self):
        """Retrieves sysupgrade driver."""
        try:
            return self.target.get_driver("SysupgradeDriver")
        except Exception:
            raise StrategyError("SysupgradeDriver not configured for this target")

    def _deactivate_drivers_after_flash(self):
        """Deactivates drivers to clear stale connections after firmware flash."""
        try:
            ssh = self.target.get_driver("SSHDriver")
            self.target.deactivate(ssh)
            logger.debug("Deactivated SSHDriver after firmware flash")
        except Exception as e:
            logger.debug(f"Could not deactivate SSH: {e}")

        try:
            self.target.deactivate(self.shell)
            logger.debug("Deactivated ShellDriver after firmware flash")
        except Exception as e:
            logger.debug(f"Could not deactivate Shell: {e}")

    def _attempt_boot_with_recovery(self, image_path):
        """Attempts to boot device with automatic recovery fallback."""
        recovery_attempt = 0

        while recovery_attempt <= self.max_recovery_attempts:
            if self._try_boot_and_configure(recovery_attempt):
                logger.info("Firmware flash and reboot completed successfully")
                return

            if self._should_attempt_recovery(recovery_attempt):
                self._perform_recovery_attempt(image_path, recovery_attempt)
                recovery_attempt += 1
            else:
                raise StrategyError("Device failed to boot after firmware flash")

    def _try_boot_and_configure(self, attempt_number):
        """
        Attempts to boot device and configure network.

        Returns:
            bool: True if successful, False if boot failed
        """
        try:
            logger.info(f"Attempting to establish shell connection (attempt {attempt_number + 1})")
            self.transition("shell")
            self.configure_libremesh_network(reboot_after=True)
            return True
        except Exception as e:
            logger.warning(f"Failed to establish shell after firmware flash: {e}")
            return False

    def _should_attempt_recovery(self, current_attempt):
        """Determines if recovery should be attempted."""
        return current_attempt < self.max_recovery_attempts and self.enable_uboot_recovery

    def _perform_recovery_attempt(self, image_path, attempt_number):
        """Performs a single U-Boot recovery attempt."""
        logger.warning(f"Starting U-Boot recovery attempt {attempt_number + 1}/{self.max_recovery_attempts}")

        try:
            self.attempt_uboot_recovery(image_path)
        except Exception as recovery_error:
            logger.error(f"U-Boot recovery attempt {attempt_number + 1} failed: {recovery_error}")

            if self._is_final_recovery_attempt(attempt_number):
                raise StrategyError(
                    f"Device failed to boot after firmware flash and {self.max_recovery_attempts} recovery attempts"
                )

    def _is_final_recovery_attempt(self, attempt_number):
        """Checks if this is the final recovery attempt."""
        return attempt_number + 1 >= self.max_recovery_attempts

    @step()
    def provision_with_firmware(self, image_path: str, keep_config: bool = False, verify_version: str = None):
        """
        Complete firmware provisioning with optional version verification.

        Args:
            image_path: Path to firmware image
            keep_config: Preserve device configuration
            verify_version: Expected firmware version string for verification

        Raises:
            StrategyError: If flash or verification fails
        """
        self.flash_firmware(image_path, keep_config=keep_config)

        if verify_version:
            self._verify_firmware_version(verify_version)

    def _verify_firmware_version(self, expected_version):
        """Verifies that the flashed firmware matches the expected version."""
        sysupgrade = self.target.get_driver("SysupgradeDriver")
        sysupgrade.verify_version(expected_version)
