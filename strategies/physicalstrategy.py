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
        try:
            self.serial_isolator = self.target.get_driver("SerialIsolatorDriver")
        except Exception:
            self.serial_isolator = None

        try:
            self.serial = self.target.get_driver("SerialDriver")
        except Exception:
            self.serial = None

    @step()
    def _check_shell_active(self):
        """Quickly checks if the shell is already active."""
        if not self.smart_state_detection:
            return False

        try:
            serial_driver = self.target.get_driver("SerialDriver")
            serial_driver.sendline("echo test_active")

            shell_check_timeout = 2
            result = serial_driver.expect(["test_active", "login:", "#"], timeout=shell_check_timeout)

            test_active_index = 0
            prompt_index = 2
            return result[0] in (test_active_index, prompt_index)
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
            self._power_on_and_wait_shell(step)
            self.target.activate(self.shell)

        self.status = state

    def _power_off(self):
        """Powers off the device physically."""
        self.target.activate(self.power)
        self.power.off()

    def _power_on_and_wait_shell(self, step):
        """Powers on the device and waits for shell to become available."""
        if self._check_shell_active():
            step.skip("Shell already active")
            return

        self.target.activate(self.power)

        if self.requires_serial_disconnect and self.serial_isolator:
            self._power_cycle_with_serial_isolation()
        else:
            self._standard_power_on()

        self._wait_for_shell()

    def _power_cycle_with_serial_isolation(self):
        """Power cycles device with serial isolation (GL-iNet specific)."""
        logger.info("Using GL-iNet serial isolator sequence")
        self.target.activate(self.serial_isolator)

        self.power.off()
        self.serial_isolator.disconnect()

        power_off_delay = 3
        sleep(power_off_delay)

        self.power.on()
        sleep(self.boot_wait)

        self.serial_isolator.connect()
        serial_reconnect_delay = 8
        sleep(serial_reconnect_delay)

    def _standard_power_on(self):
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
        shell_ready_timeout = 5
        retry_interval = 2

        while _time_mod() - start_time < self.connection_timeout:
            try:
                result = self.shell.run("echo ready", timeout=shell_ready_timeout)
                exit_code_index = 2
                stdout_index = 0

                if result[exit_code_index] == 0 and result[stdout_index] and "ready" in result[stdout_index][0]:
                    return
            except Exception:
                pass

            sleep(retry_interval)

        raise StrategyError(f"Shell not ready after {self.connection_timeout}s")

    def _wake_console(self):
        """Sends newlines to wake up the console."""
        if not self.serial:
            return

        self.target.activate(self.serial)
        wake_attempts = 3
        wake_delay = 0.2

        for _ in range(wake_attempts):
            try:
                self.serial.sendline("")
            except Exception:
                pass
            sleep(wake_delay)

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
                self._try_ssh_shutdown()
        finally:
            self.ensure_off()
            logger.info("Physical power off completed")

    def _try_ssh_shutdown(self):
        """Attempts clean shutdown via SSH."""
        try:
            ssh = self.target.get_driver("SSHDriver")
            logger.info("Attempting clean shutdown via SSH")

            shutdown_timeout = 10
            ssh.run("poweroff", timeout=shutdown_timeout)

            max_shutdown_wait_cycles = 8
            shutdown_check_interval = 2
            connection_check_timeout = 3

            for cycle in range(max_shutdown_wait_cycles):
                try:
                    ssh.run("true", timeout=connection_check_timeout)
                    sleep(shutdown_check_interval)
                except Exception:
                    elapsed_time = (cycle + 1) * shutdown_check_interval
                    logger.info(f"SSH stopped responding after {elapsed_time}s - clean shutdown completed")
                    return

            max_wait_time = max_shutdown_wait_cycles * shutdown_check_interval
            logger.warning(f"SSH still responding after {max_wait_time}s, will force physical shutdown")

        except Exception as e:
            logger.warning(f"SSH shutdown failed: {e}, using physical power off")

    @step()
    def configure_libremesh_network(self):
        """
        Configures LibreMesh network to enable SSH access from test infrastructure.

        LibreMesh defaults to static IP configuration. This method configures
        the LAN interface to use DHCP, allowing the device to obtain an IP
        address from the test network.

        Tries SSH first (preferred), falls back to serial console if unavailable.
        """
        logger.info("Configuring LibreMesh network interface for testbed access")

        if self._try_configure_network_via_ssh():
            return

        self._configure_network_via_serial()

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
            network_config_commands = [
                "uci set network.lan.proto='dhcp'",
                "uci commit network",
                "/etc/init.d/network restart",
            ]

            command_timeout = 30
            for cmd in network_config_commands:
                try:
                    result = ssh.run(cmd, timeout=command_timeout)
                    logger.debug(f"Network config command (SSH) '{cmd}' exited with code {result[2]}")
                except Exception as e:
                    logger.warning(f"Network config command (SSH) '{cmd}' failed: {e}")

            logger.info(f"Waiting {self.post_dhcp_wait}s for network reconfiguration")
            sleep(self.post_dhcp_wait)

            logger.info("Network configuration via SSH completed")
            return True

        except Exception as e:
            logger.info(f"SSH not available for network config, falling back to serial: {e}")
            return False

    def _configure_network_via_serial(self):
        """Configures network via serial console (fallback method)."""
        self.target.activate(self.shell)

        try:
            serial = self.target.get_driver("SerialDriver")
            logger.debug("Flushing serial buffer before network config")
            serial.sendline("")
            buffer_flush_delay = 0.5
            sleep(buffer_flush_delay)
        except Exception as e:
            logger.warning(f"Could not flush serial buffer: {e}")

        network_config_commands = [
            'uci set network.lan.proto=dhcp',
            'uci commit network',
            '/etc/init.d/network restart',
        ]

        command_timeout = 10
        for cmd in network_config_commands:
            try:
                logger.debug(f"Sending network config command: {cmd}")
                serial.sendline(cmd)
                sleep(self.network_config_retry_wait)

                try:
                    serial.expect('#', timeout=command_timeout)
                    logger.debug(f"Network config command '{cmd}' sent successfully")
                except Exception as e:
                    logger.debug(f"Expect after '{cmd}' had issues (non-fatal): {e}")

            except Exception as e:
                logger.warning(f"Network config command '{cmd}' failed: {e}")

        logger.info(f"Waiting {self.post_dhcp_wait}s for network reconfiguration")
        sleep(self.post_dhcp_wait)
        logger.info("Network configuration via serial completed")

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

        initramfs_filename = self._get_initramfs_filename(uboot, firmware_image)
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

    def _get_initramfs_filename(self, uboot, firmware_image):
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
            logger.warning(f"Initramfs not found: {initramfs_path}, will try existing file in TFTP root")
            if not os.path.exists(tftp_initramfs_path):
                raise StrategyError(f"Initramfs not found in images dir or TFTP root: {initramfs_filename}")

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
            shutil.copy2(source_path, dest_path)
            tftp_file_permissions = 0o644
            os.chmod(dest_path, tftp_file_permissions)
        except PermissionError:
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
        try:
            if self.shell in self.target.active:
                self.target.deactivate(self.shell)
        except Exception:
            pass

        try:
            ssh = self.target.get_driver("SSHDriver", activate=False)
            if ssh in self.target.active:
                self.target.deactivate(ssh)
        except Exception:
            pass

    def _boot_initramfs_from_uboot(self, uboot, serial):
        """Boots initramfs from U-Boot via TFTP."""
        self.target.activate(serial)

        logger.info("Powering on device for U-Boot access")
        self.power.on()

        self._interrupt_uboot(serial)
        self._activate_and_boot_uboot(uboot)
        self._wait_for_shell_after_uboot_boot(uboot)

    def _interrupt_uboot(self, serial):
        """Sends interrupt characters to catch U-Boot bootloader."""
        logger.info(f"Sending {self.uboot_interrupt_count} interrupt characters to catch U-Boot")

        for _ in range(self.uboot_interrupt_count):
            sleep(self.uboot_interrupt_delay)
            serial.write(b' ')

        logger.info("Waiting for U-Boot prompt")

    def _activate_and_boot_uboot(self, uboot):
        """Activates U-Boot and boots the loaded initramfs."""
        self.target.activate(uboot)
        logger.info("U-Boot prompt acquired, TFTP download should be in progress")

        logger.info("Booting initramfs from RAM...")
        uboot.boot("")
        uboot.await_boot()

        logger.info("U-Boot recovery: device booted from RAM, waiting for shell...")
        self.target.deactivate(uboot)

    def _wait_for_shell_after_uboot_boot(self, uboot):
        """Waits for Linux shell to become available after U-Boot boot."""
        logger.info(f"Waiting up to {self.uboot_boot_wait}s for Linux shell after RAM boot...")
        self.status = Status.unknown

        for _ in range(self.uboot_boot_wait):
            try:
                if self._check_shell_active():
                    self.target.activate(self.shell)
                    logger.info("Shell access acquired after initramfs RAM boot")
                    return
            except Exception:
                pass
            sleep(1)

        raise StrategyError("Failed to get shell after U-Boot initramfs boot")

    def _persist_sysupgrade_firmware(self, firmware_image, uboot):
        """Persists sysupgrade firmware to flash after RAM boot."""
        logger.info("Persisting sysupgrade firmware to flash...")

        firmware_path = self._upload_sysupgrade_firmware(firmware_image, uboot)
        self._run_sysupgrade(firmware_path)
        self._wait_for_reboot_after_sysupgrade()

    def _upload_sysupgrade_firmware(self, firmware_image, uboot):
        """
        Uploads sysupgrade firmware to device.

        Returns:
            str: Path to firmware on device
        """
        try:
            return self._upload_via_ssh(firmware_image)
        except Exception as e:
            logger.warning(f"SSH not available for upload: {e}")
            return self._download_via_tftp(firmware_image, uboot)

    def _upload_via_ssh(self, firmware_image):
        """Uploads firmware via SSH."""
        ssh = self.target.get_driver("SSHDriver", activate=False)
        self.target.activate(ssh)

        logger.info("SSH available, uploading sysupgrade firmware for persistence")
        device_firmware_path = "/tmp/recovery_sysupgrade.bin"
        ssh.put(firmware_image, device_firmware_path)
        return device_firmware_path

    def _download_via_tftp(self, firmware_image, uboot):
        """Downloads firmware via TFTP (fallback method)."""
        firmware_basename = os.path.basename(firmware_image)
        tftp_sysupgrade_path = os.path.join(self.tftp_root, firmware_basename)

        if not os.path.exists(tftp_sysupgrade_path):
            logger.info(f"Copying sysupgrade to TFTP root: {tftp_sysupgrade_path}")
            shutil.copy2(firmware_image, tftp_sysupgrade_path)
            os.system(f"sudo chown tftp:tftp {shlex.quote(tftp_sysupgrade_path)}")
            os.system(f"sudo chmod 644 {shlex.quote(tftp_sysupgrade_path)}")

        logger.info("Trying to download sysupgrade via serial from TFTP...")

        serverip = self._extract_tftp_server_ip(uboot)
        logger.info(f"Downloading from TFTP server {serverip}")

        try:
            tftp_download_timeout = 60
            self.shell.run(
                f"cd /tmp && tftp -g -r {firmware_basename} {serverip}",
                timeout=tftp_download_timeout
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

    def _run_sysupgrade(self, firmware_path):
        """Executes sysupgrade command to persist firmware."""
        logger.info(f"Running sysupgrade -n -F {firmware_path} to persist firmware...")

        try:
            self.shell.console.sendline(f"sysupgrade -n -F {firmware_path}")
            logger.info("Sysupgrade command sent, device is rebooting...")
        except Exception as e:
            logger.info(f"Shell closed during sysupgrade (expected): {e}")

    def _wait_for_reboot_after_sysupgrade(self):
        """Waits for device to reboot after sysupgrade."""
        reboot_start_delay = 10
        logger.info("Waiting for device to reboot after persistence...")
        sleep(reboot_start_delay)

        try:
            self.target.deactivate(self.shell)
        except Exception:
            pass

        try:
            ssh = self.target.get_driver("SSHDriver", activate=False)
            if ssh in self.target.active:
                self.target.deactivate(ssh)
        except Exception:
            pass

        self.status = Status.unknown

    @step()
    def flash_firmware(self, image_path: str, keep_config: bool = False):
        """
        Flashes firmware and waits for device to boot.

        If boot fails and U-Boot recovery is enabled, attempts automatic recovery.

        Args:
            image_path: Path to firmware image file
            keep_config: If True, preserves device configuration

        Raises:
            StrategyError: If flash and all recovery attempts fail
        """
        if self.status != Status.shell:
            self.transition("shell")

        sysupgrade = self._get_sysupgrade_driver()
        sysupgrade.flash(image_path, keep_config=keep_config)

        self._deactivate_drivers_after_flash()
        self.status = Status.unknown

        self._attempt_boot_with_recovery(image_path)

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
            try:
                logger.info(f"Attempting to establish shell connection (attempt {recovery_attempt + 1})")
                self.transition("shell")
                self.configure_libremesh_network()

                logger.info("Firmware flash and reboot completed successfully")
                return

            except Exception as e:
                logger.warning(f"Failed to establish shell after firmware flash: {e}")

                if self._should_attempt_recovery(recovery_attempt):
                    self._perform_recovery_attempt(image_path, recovery_attempt)
                    recovery_attempt += 1
                else:
                    raise StrategyError(f"Device failed to boot after firmware flash: {e}")

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

            if attempt_number + 1 >= self.max_recovery_attempts:
                raise StrategyError(
                    f"Device failed to boot after firmware flash and {self.max_recovery_attempts} recovery attempts"
                )

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
            sysupgrade = self.target.get_driver("SysupgradeDriver")
            sysupgrade.verify_version(verify_version)
