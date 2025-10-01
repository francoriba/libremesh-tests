import attr
import logging
from time import sleep
from labgrid import target_factory
from labgrid.driver import Driver
from labgrid.step import step
from labgrid.strategy import StrategyError

logger = logging.getLogger(__name__)


@target_factory.reg_driver
@attr.s(eq=False)
class SysupgradeDriver(Driver):
    """
    Driver for flashing OpenWrt/LibreMesh firmware via sysupgrade.
    
    This driver handles the complete firmware upgrade process including:
    - Uploading firmware image via SSH/SCP
    - Validating image compatibility with sysupgrade -T
    - Executing sysupgrade command
    - Waiting for device reboot
    
    Attributes:
        keep_config: If True, preserves device configuration during upgrade
        force: If True, forces upgrade even with version mismatches (-F flag)
        verify_boot_timeout: Seconds to wait after triggering sysupgrade
    """
    
    bindings = {
        "shell": "ShellDriver",
    }
    
    keep_config = attr.ib(default=False, validator=attr.validators.instance_of(bool))
    force = attr.ib(default=True, validator=attr.validators.instance_of(bool))
    verify_boot_timeout = attr.ib(default=120, validator=attr.validators.instance_of(int))
    
    @step(title="sysupgrade", args=["image_path"])
    def flash(self, image_path: str, keep_config: bool = None, validate: bool = True):
        """
        Flashes firmware image using sysupgrade.
        
        Args:
            image_path: Local path to firmware image file
            keep_config: If True, preserves configuration (overrides default)
            validate: If True, validates image compatibility before flashing
            
        Raises:
            StrategyError: If upload, validation, or flash operation fails
        """
        if keep_config is None:
            keep_config = self.keep_config
            
        remote_path = "/tmp/sysupgrade.bin"
        
        # Get SSH driver manually to avoid automatic activation dependency
        try:
            ssh = self.target.get_driver("SSHDriver")
            self.target.activate(ssh)
        except Exception as e:
            raise StrategyError(f"Cannot upload firmware without SSH: {e}")
        
        logger.info(f"Uploading firmware image to {remote_path}")
        ssh.put(image_path, remote_path)
        
        # Verify upload success
        result = ssh.run(f"ls -lh {remote_path}")
        if result[2] != 0:
            raise StrategyError(f"Failed to upload firmware to {remote_path}")
        
        logger.debug(f"Firmware uploaded: {result[0]}")
        
        # Validate image compatibility
        if validate:
            logger.info("Validating firmware image compatibility")
            result = ssh.run(f"sysupgrade -T {remote_path}")
            if result[2] != 0:
                error_msg = "\n".join(result[0]) if result[0] else "Unknown error"
                raise StrategyError(f"Firmware validation failed: {error_msg}")
            logger.info("Firmware image validation passed")
        
        # Build sysupgrade command
        cmd_parts = ["sysupgrade"]
        
        if not keep_config:
            cmd_parts.append("-n")
            
        if self.force:
            cmd_parts.append("-F")
            
        cmd_parts.append(remote_path)
        cmd = " ".join(cmd_parts)
        
        logger.info(f"Executing sysupgrade command: {cmd}")
        
        # Execute sysupgrade (connection will drop during reboot)
        try:
            ssh.run(cmd, timeout=10)
        except Exception:
            # SSH connection closes when device reboots - this is expected
            pass
        
        logger.info(f"Waiting {self.verify_boot_timeout}s for device reboot")
        sleep(self.verify_boot_timeout)
        
    @step(title="verify_firmware", args=["expected_version"])
    def verify_version(self, expected_version: str = None):
        """
        Verifies the flashed firmware version.
        
        Args:
            expected_version: Expected version string (e.g., "23.05.5")
            
        Raises:
            StrategyError: If version cannot be read or doesn't match expected
        """
        self.target.activate(self.shell)
        
        result = self.shell.run("cat /etc/openwrt_release")
        if result[2] != 0:
            raise StrategyError("Failed to read firmware version")
            
        release_info = "\n".join(result[0])
        logger.info(f"Current firmware version:\n{release_info}")
        
        if expected_version:
            version_found = expected_version in release_info
            if not version_found:
                raise StrategyError(
                    f"Firmware version mismatch. Expected '{expected_version}' in:\n{release_info}"
                )
            logger.info(f"Firmware version verified: {expected_version}")
