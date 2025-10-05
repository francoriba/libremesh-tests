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

    requires_serial_disconnect = attr.ib(default=False)
    boot_wait = attr.ib(default=20)
    connection_timeout = attr.ib(default=60)
    smart_state_detection = attr.ib(default=True)
    status = attr.ib(default=Status.unknown)
    enable_uboot_recovery = attr.ib(default=False)
    max_recovery_attempts = attr.ib(default=2)
    tftp_root = attr.ib(default="/srv/tftp")  # TFTP local directory

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        try:
            self.serial_isolator = self.target.get_driver("SerialIsolatorDriver")
        except Exception:
            self.serial_isolator = None
        # SerialDriver for quick commands
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
            result = serial_driver.expect(["test_active", "login:", "#"], timeout=2)
            return result[0] in (0, 2)  # test_active or prompt
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
            # activate the ShellDriver even if we skip
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
        """Powers on the device and waits for shell."""
        # Check if already active
        if self._check_shell_active():
            step.skip("Shell already active")
            return

        # Power cycle (with GL-iNet quirk if needed)
        self.target.activate(self.power)

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
            # Standard boot for Belkin
            logger.info("Using standard power-on sequence")
            self.power.on()
            sleep(self.boot_wait)

        # Wait for shell
        self._wait_for_shell()

    @step()
    def _wait_for_shell(self):
        """Waits for the shell to be ready."""
        # Wake up console if needed
        if self.serial:
            self.target.activate(self.serial)
            for _ in range(3):
                try:
                    self.serial.sendline("")  # Send Enter
                except Exception:
                    pass
                sleep(0.2)

        # Activate shell and wait
        self.target.activate(self.shell)

        start_time = _time_mod()
        while _time_mod() - start_time < self.connection_timeout:
            try:
                result = self.shell.run("echo ready", timeout=5)
                if result[2] == 0 and result[0] and "ready" in result[0][0]:
                    return  # Shell ready!
            except Exception:
                pass  # Continue trying

            sleep(2)

        raise StrategyError(f"Shell not ready after {self.connection_timeout}s")

    @step()
    def force_power_cycle(self):
        """Forces a complete power cycle, ignoring smart detection."""
        original_smart = self.smart_state_detection
        try:
            self.smart_state_detection = False
            self.status = Status.unknown
            self.transition("shell")
        finally:
            self.smart_state_detection = original_smart

    @step()
    def ensure_off(self):
        """Ensures the device is powered off."""
        if self.status != Status.off:
            self.transition("off")

    @step()
    def cleanup_and_shutdown(self):
        """Clean shutdown via SSH + guaranteed physical power off."""
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
            ssh.run("poweroff", timeout=10)

            # Wait for SSH to stop responding (max 15s)
            for i in range(8):  # 8 * 2s = 16s maximum
                try:
                    ssh.run("true", timeout=3)
                    sleep(2)
                except Exception:
                    logger.info(f"SSH stopped responding after {(i + 1) * 2}s - clean shutdown completed")
                    return

            logger.warning("SSH still responding after 16s, will force physical shutdown")

        except Exception as e:
            logger.warning(f"SSH shutdown failed: {e}, using physical power off")

    @step()
    def configure_libremesh_network(self):
        """
        Configures LibreMesh network to enable SSH access from test infrastructure.

        LibreMesh defaults to static IP configuration. This method configures
        the LAN interface to use DHCP, allowing the device to obtain an IP
        address from the test network.

        Executes via serial console (ShellDriver) to avoid SSH dependency.
        """
        logger.info("Configuring LibreMesh network interface for testbed access")

        self.target.activate(self.shell)

        # Configure LAN interface for DHCP
        commands = [
            "uci set network.lan.proto='dhcp'",
            "uci commit network",
            "/etc/init.d/network restart",
        ]

        for cmd in commands:
            try:
                result = self.shell.run(cmd, timeout=10)
                logger.debug(f"Network config command '{cmd}' exited with code {result[2]}")
            except Exception as e:
                logger.warning(f"Network config command '{cmd}' failed: {e}")

        # Allow time for DHCP negotiation and network restart
        logger.info("Waiting for network reconfiguration")
        sleep(15)

        logger.info("Network configuration completed")

    @step()
    def attempt_uboot_recovery(self, firmware_image: str):
        """
        Attempts to recover device using U-Boot and TFTP.
        
        This method is called when normal boot fails after firmware flash.
        It power cycles the device, interrupts U-Boot, loads firmware via TFTP,
        and attempts to boot.
        
        Args:
            firmware_image: Path to firmware image for recovery
            
        Raises:
            StrategyError: If U-Boot recovery fails
        """
        import os
        import shutil
        from time import sleep
        
        logger.warning("Attempting U-Boot recovery")
        
        # Check if U-Boot is configured (without activating it yet)
        try:
            uboot = self.target.get_driver("UBootDriver", activate=False)
        except Exception as e:
            raise StrategyError(f"U-Boot recovery not configured: {e}")
        
        # Get SerialDriver (without activating it yet)
        try:
            serial = self.target.get_driver("SerialDriver", activate=False)
        except Exception as e:
            raise StrategyError(f"SerialDriver not available: {e}")
        
        # Copy firmware to TFTP root if not already there
        firmware_basename = os.path.basename(firmware_image)
        tftp_image_path = os.path.join(self.tftp_root, firmware_basename)
        
        if not os.path.exists(tftp_image_path) or os.path.getmtime(firmware_image) > os.path.getmtime(tftp_image_path):
            logger.info(f"Copying firmware to TFTP root: {tftp_image_path}")
            shutil.copy2(firmware_image, tftp_image_path)
            # Set proper permissions for TFTP server
            os.system(f"sudo chown tftp:tftp {tftp_image_path}")
            os.system(f"sudo chmod 644 {tftp_image_path}")
        
        # Ensure device is completely off before attempting U-Boot access
        logger.info("Ensuring device is powered off")
        self.target.activate(self.power)
        self.power.off()
        sleep(5)  # Longer wait to ensure complete power down
        
        # Deactivate any active drivers to clear state
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
        
        # Activate serial for U-Boot capture BEFORE powering on
        self.target.activate(serial)
        
        # Power on
        logger.info("Powering on device for U-Boot access")
        self.power.on()
        
        # bomb the serial with whitespaces to interrupt autoboot
        logger.info("Sending interrupt characters to catch U-Boot")
        for i in range(15):
            sleep(0.2)
            serial.write(b' ')
        
        logger.info("Waiting for U-Boot prompt")
        
        try:
            # Activate U-Boot (will wait for prompt)
            self.target.activate(uboot)
            logger.info("U-Boot prompt acquired")
            
            # Set bootfile variable to our firmware
            uboot.run(f"setenv bootfile {firmware_basename}")
            
            # Execute init_commands (will do TFTP download)
            logger.info(f"Loading firmware via TFTP: {firmware_basename}")
            # init_commands are executed automatically when UBootDriver activates
            
            # Boot loaded image
            logger.info("Booting recovered firmware...")
            uboot.boot("")
            uboot.await_boot()
            
            logger.info("U-Boot recovery boot initiated successfully")
            
        except Exception as e:
            raise StrategyError(f"U-Boot recovery failed: {e}")

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
        # Ensure device is accessible
        if self.status != Status.shell:
            self.transition("shell")
        
        # Get sysupgrade driver
        try:
            sysupgrade = self.target.get_driver("SysupgradeDriver")
        except Exception:
            raise StrategyError("SysupgradeDriver not configured for this target")
        
        # Perform firmware flash
        sysupgrade.flash(image_path, keep_config=keep_config)
        
        # Deactivate drivers to clear stale connections after reboot
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
        
        # Try to re-establish connection after reboot
        self.status = Status.unknown
        
        recovery_attempt = 0
        while recovery_attempt <= self.max_recovery_attempts:
            try:
                # Attempt normal boot
                logger.info(f"Attempting to establish shell connection (attempt {recovery_attempt + 1})")
                self.transition("shell")
                
                # Configure network for LibreMesh
                self.configure_libremesh_network()
                
                logger.info("Firmware flash and reboot completed successfully")
                return
                
            except Exception as e:
                logger.warning(f"Failed to establish shell after firmware flash: {e}")
                
                if recovery_attempt < self.max_recovery_attempts and self.enable_uboot_recovery:
                    logger.warning(f"Starting U-Boot recovery attempt {recovery_attempt + 1}/{self.max_recovery_attempts}")
                    
                    try:
                        self.attempt_uboot_recovery(image_path)
                        recovery_attempt += 1
                        # Loop will retry normal boot after recovery
                        
                    except Exception as recovery_error:
                        logger.error(f"U-Boot recovery attempt {recovery_attempt + 1} failed: {recovery_error}")
                        recovery_attempt += 1
                        
                        if recovery_attempt >= self.max_recovery_attempts:
                            raise StrategyError(
                                f"Device failed to boot after firmware flash and {self.max_recovery_attempts} recovery attempts"
                            )
                else:
                    raise StrategyError(f"Device failed to boot after firmware flash: {e}")

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
