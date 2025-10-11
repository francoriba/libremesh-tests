import attr
import logging
import hashlib
import os
from time import sleep
from labgrid import target_factory
from labgrid.driver import Driver
from labgrid.step import step
from labgrid.strategy import StrategyError

logger = logging.getLogger(__name__)

BYTES_PER_KILOBYTE = 1024
BYTES_PER_MEGABYTE = 1024 * 1024
FILE_READ_CHUNK_SIZE = 4096
DEFAULT_TMP_SPACE_MARGIN_MB = 2
DEFAULT_REMOTE_PATH = "/tmp/sysupgrade.bin"
DEFAULT_BOOT_TIMEOUT_SECONDS = 120
SYSUPGRADE_COMMAND_TIMEOUT = 10

SYSINFO_BOARD_PATH = "/tmp/sysinfo/board_name"
OPENWRT_RELEASE_PATH = "/etc/openwrt_release"

KEEP_CONFIG_FLAG = "-n"
FORCE_DOWNGRADE_FLAG = "-F"
VALIDATE_IMAGE_FLAG = "-T"

LS_SIZE_FIELD_INDEX = 4
DF_AVAILABLE_FIELD_INDEX = 3
SHA256_HASH_FIELD_INDEX = 0


@target_factory.reg_driver
@attr.s(eq=False)
class SysupgradeDriver(Driver):
    """
    Driver for flashing OpenWrt/LibreMesh firmware via sysupgrade.

    This driver handles the complete firmware upgrade process including:
    - Board compatibility validation
    - SHA256 checksum verification (local and remote)
    - File size validation
    - Free space checks
    - Uploading firmware image via SSH/SCP
    - Validating image compatibility with sysupgrade -T
    - Executing sysupgrade command
    - Waiting for device reboot

    Attributes:
        keep_config: If True, preserves device configuration during upgrade
        force: If True, forces upgrade (-F flag). Deprecated, use allow_downgrade
        allow_downgrade: If True, allows downgrading firmware (-F flag)
        verify_boot_timeout: Seconds to wait after triggering sysupgrade
        expected_board: Expected board name (e.g., "linksys,e8450-ubi")
        skip_if_installed: If True, skip flash if same version already installed
        tmp_space_margin_mb: Extra space margin in MB for /tmp (default: 2)
        remote_path: Remote path for temporary firmware storage (default: /tmp/sysupgrade.bin)
        validate_only: If True, run all checks but don't flash (CI mode)
    """

    bindings = {
        "shell": "ShellDriver",
    }

    keep_config = attr.ib(default=False, validator=attr.validators.instance_of(bool))
    force = attr.ib(default=False, validator=attr.validators.instance_of(bool))
    allow_downgrade = attr.ib(default=None)
    verify_boot_timeout = attr.ib(default=DEFAULT_BOOT_TIMEOUT_SECONDS, validator=attr.validators.instance_of(int))
    expected_board = attr.ib(default=None)
    skip_if_installed = attr.ib(default=False, validator=attr.validators.instance_of(bool))
    tmp_space_margin_mb = attr.ib(default=DEFAULT_TMP_SPACE_MARGIN_MB, validator=attr.validators.instance_of(int))
    remote_path = attr.ib(default=DEFAULT_REMOTE_PATH, validator=attr.validators.instance_of(str))
    validate_only = attr.ib(default=False, validator=attr.validators.instance_of(bool))

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        if self.allow_downgrade is None:
            self.allow_downgrade = self.force

    def _check_sysupgrade_available(self):
        """
        Verifies that sysupgrade is available on the device.

        Raises:
            StrategyError: If sysupgrade is not available
        """
        stdout, stderr, exit_code = self.shell.run("command -v sysupgrade")
        if exit_code != 0 or not stdout:
            raise StrategyError(
                "`sysupgrade` is not available on the device. "
                "You may be in initramfs or using vendor firmware."
            )
        logger.debug("sysupgrade command is available")

    def _get_board_name(self) -> str:
        """
        Retrieves board name from device.

        Returns:
            str: Board name

        Raises:
            StrategyError: If board name cannot be retrieved
        """
        result = self.shell.run("cat /tmp/sysinfo/board_name")
        stdout, stderr, exit_code = result

        if exit_code != 0:
            raise StrategyError(
                f"Failed to read board name (exit code {exit_code}): {stderr}"
            )

        if not stdout:
            raise StrategyError("No board name returned from device")

        # Filter out kernel messages that may appear in stdout
        board_lines = [
            line.strip()
            for line in stdout
            if line.strip() and not line.strip().startswith('[')
        ]

        if not board_lines:
            raise StrategyError(
                f"Could not extract board name from output (only kernel messages found): {stdout}"
            )

        board_name = board_lines[0]
        return board_name

    def _calculate_sha256(self, file_path: str) -> str:
        """
        Calculates SHA256 hash of a local file.

        Args:
            file_path: Path to local file

        Returns:
            str: SHA256 hash in hexadecimal
        """
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as file:
            for chunk in iter(lambda: file.read(FILE_READ_CHUNK_SIZE), b""):
                sha256_hash.update(chunk)
        return sha256_hash.hexdigest()

    def _get_file_size(self, file_path: str) -> int:
        """
        Gets the size of a local file in bytes.

        Args:
            file_path: Path to local file

        Returns:
            int: File size in bytes
        """
        return os.path.getsize(file_path)

    def _get_remote_sha256(self, ssh_driver, remote_path: str) -> str:
        """
        Calculates SHA256 hash of a remote file on the device.

        Args:
            ssh_driver: SSHDriver instance
            remote_path: Path to remote file

        Returns:
            str: SHA256 hash in hexadecimal

        Raises:
            StrategyError: If hash cannot be calculated
        """
        stdout, stderr, exit_code = ssh_driver.run(f"sha256sum {remote_path}")
        if exit_code != 0:
            stdout_text = "\n".join((stdout or []))
            stderr_text = "\n".join((stderr or []))
            raise StrategyError(
                f"Failed to calculate SHA256 on device for {remote_path}\n"
                f"stdout: {stdout_text}\nstderr: {stderr_text}"
            )

        if not stdout:
            raise StrategyError(f"sha256sum returned no output for {remote_path}")

        try:
            hash_value = stdout[0].split()[SHA256_HASH_FIELD_INDEX]
            return hash_value
        except (IndexError, AttributeError) as error:
            raise StrategyError(f"Failed to parse sha256sum output: {error}")

    def _get_remote_file_size(self, ssh_driver, remote_path: str) -> int:
        """
        Gets the size of a remote file on the device.

        Args:
            ssh_driver: SSHDriver instance
            remote_path: Path to remote file

        Returns:
            int: File size in bytes

        Raises:
            StrategyError: If size cannot be retrieved
        """
        stdout, stderr, exit_code = ssh_driver.run(f"stat -c %s {remote_path}")
        if exit_code == 0 and stdout:
            try:
                return int(stdout[0].strip())
            except (ValueError, IndexError):
                pass

        stdout, stderr, exit_code = ssh_driver.run(f"ls -l {remote_path}")
        if exit_code != 0:
            raise StrategyError(f"Failed to get file size for {remote_path}")

        try:
            fields = stdout[0].split()
            file_size = int(fields[LS_SIZE_FIELD_INDEX])
            return file_size
        except (ValueError, IndexError) as error:
            raise StrategyError(f"Failed to parse file size from ls output: {error}")

    def _check_tmp_space(self, ssh_driver, required_bytes: int):
        """
        Checks if /tmp has enough free space.

        Args:
            ssh_driver: SSHDriver instance
            required_bytes: Required space in bytes

        Raises:
            StrategyError: If insufficient space available
        """
        stdout, stderr, exit_code = ssh_driver.run("df -P /tmp | tail -1")
        if exit_code != 0:
            raise StrategyError("Failed to check /tmp free space")

        fields = stdout[0].split()
        available_kilobytes = int(fields[DF_AVAILABLE_FIELD_INDEX])
        available_bytes = available_kilobytes * BYTES_PER_KILOBYTE

        margin_bytes = self.tmp_space_margin_mb * BYTES_PER_MEGABYTE
        required_with_margin = required_bytes + margin_bytes

        available_mb = available_bytes / BYTES_PER_MEGABYTE
        required_mb = required_with_margin / BYTES_PER_MEGABYTE

        logger.info(
            f"/tmp space: {available_mb:.1f} MB available, "
            f"{required_mb:.1f} MB required (with {self.tmp_space_margin_mb} MB margin)"
        )

        if available_bytes < required_with_margin:
            raise StrategyError(
                f"Insufficient space in /tmp: {available_mb:.1f} MB available, "
                f"{required_mb:.1f} MB required"
            )

    def _warn_ubi_mismatch(self, image_path: str):
        """
        Warns if board expects UBI but image doesn't appear to be UBI.

        Args:
            image_path: Path to firmware image
        """
        if not self.expected_board:
            return

        board_is_ubi = self.expected_board.endswith("-ubi") or "-ubi" in self.expected_board
        image_filename = os.path.basename(image_path).lower()
        image_is_ubi = "-ubi-" in image_filename

        if board_is_ubi and not image_is_ubi:
            logger.warning(
                f"Board '{self.expected_board}' appears to be UBI, but image "
                f"'{os.path.basename(image_path)}' does not contain '-ubi-' in filename. "
                f"This may cause sysupgrade -T validation to fail."
            )

    def _get_installed_version(self):
        """
        Retrieves the currently installed firmware version.

        Returns:
            dict: Dictionary with version info (DISTRIB_ID, DISTRIB_RELEASE, etc.)

        Raises:
            StrategyError: If version cannot be retrieved
        """
        stdout, stderr, exit_code = self.shell.run(f"cat {OPENWRT_RELEASE_PATH}")
        if exit_code != 0:
            raise StrategyError("Failed to read installed firmware version")

        version_info = {}
        for line in stdout:
            if '=' in line:
                key, value = line.split('=', 1)
                version_info[key] = value.strip().strip("'\"")

        return version_info

    @step(title="validate_board", args=["image_path"])
    def validate_board(self, image_path: str):
        """
        Validates that firmware image is compatible with device board.

        Args:
            image_path: Path to firmware image

        Raises:
            StrategyError: If board validation fails
        """
        if not self.expected_board:
            logger.warning("No expected_board configured, skipping board validation")
            return

        board_name = self._get_board_name()
        logger.info(f"Device board: {board_name}")

        if board_name != self.expected_board:
            raise StrategyError(
                f"Board mismatch! Device: '{board_name}', Expected: '{self.expected_board}'. "
                f"Refusing to flash incompatible firmware."
            )

        logger.info(f"Board validation passed: {board_name}")
        self._warn_ubi_mismatch(image_path)

    @step(title="verify_local_checksum", args=["image_path", "expected_sha256"])
    def verify_local_checksum(self, image_path: str, expected_sha256: str = None):
        """
        Verifies SHA256 checksum of local firmware image.

        Args:
            image_path: Path to firmware image
            expected_sha256: Expected SHA256 hash (if None, just logs the hash)

        Raises:
            StrategyError: If checksum doesn't match expected value
        """
        actual_sha256 = self._calculate_sha256(image_path)
        logger.info(f"Local firmware SHA256: {actual_sha256}")

        if expected_sha256:
            if actual_sha256 != expected_sha256:
                raise StrategyError(
                    f"Local SHA256 mismatch! Got: {actual_sha256}, Expected: {expected_sha256}"
                )
            logger.info("Local SHA256 checksum verified")

        return actual_sha256

    @step(title="verify_remote_integrity", args=["remote_path", "expected_sha256", "expected_size"])
    def verify_remote_integrity(self, ssh_driver, remote_path: str, expected_sha256: str, expected_size: int):
        """
        Verifies integrity of uploaded file on device.

        Args:
            ssh_driver: SSHDriver instance
            remote_path: Path to remote file
            expected_sha256: Expected SHA256 hash
            expected_size: Expected file size in bytes

        Raises:
            StrategyError: If integrity check fails
        """
        remote_size = self._get_remote_file_size(ssh_driver, remote_path)
        logger.info(f"Remote file size: {remote_size} bytes (expected: {expected_size} bytes)")

        if remote_size != expected_size:
            raise StrategyError(
                f"Remote file size mismatch! Got: {remote_size} bytes, Expected: {expected_size} bytes. "
                f"Upload may be truncated or corrupted."
            )

        remote_sha256 = self._get_remote_sha256(ssh_driver, remote_path)
        logger.info(f"Remote firmware SHA256: {remote_sha256}")

        if remote_sha256 != expected_sha256:
            raise StrategyError(
                f"Remote SHA256 mismatch! Got: {remote_sha256}, Expected: {expected_sha256}. "
                f"Upload corrupted."
            )

        logger.info("Remote file integrity verified: size and SHA256 match")

    @step(title="check_if_installed", args=["expected_version"])
    def check_if_installed(self, expected_version: str = None) -> bool:
        """
        Checks if a specific firmware version is already installed.

        Args:
            expected_version: Version string to check (e.g., "2024.1")

        Returns:
            bool: True if version is already installed, False otherwise
        """
        if not expected_version:
            return False

        try:
            version_info = self._get_installed_version()

            release = version_info.get('DISTRIB_RELEASE', '')
            revision = version_info.get('DISTRIB_REVISION', '')

            if expected_version in release or expected_version in revision:
                logger.info(f"Version {expected_version} already installed")
                return True

            logger.info(f"Different version installed: {release} (rev: {revision})")
            return False

        except Exception as error:
            logger.warning(f"Could not check installed version: {error}")
            return False

    @step(title="sysupgrade", args=["image_path"])
    def flash(self, image_path: str, keep_config: bool = None, validate: bool = True,
              expected_sha256: str = None, expected_version: str = None,
              validate_only: bool = None):
        """
        Flashes firmware image using sysupgrade with comprehensive safety checks.

        Args:
            image_path: Local path to firmware image file
            keep_config: If True, preserves configuration (overrides default)
            validate: If True, validates image compatibility before flashing
            expected_sha256: Expected SHA256 hash for verification
            expected_version: Expected version (for skip_if_installed check)
            validate_only: If True, run all checks but don't flash (CI mode)

        Raises:
            StrategyError: If safety checks or flash operation fails
        """
        if keep_config is None:
            keep_config = self.keep_config

        if validate_only is None:
            validate_only = self.validate_only

        self.target.activate(self.shell)

        self._check_sysupgrade_available()
        self.validate_board(image_path)

        local_sha256 = self.verify_local_checksum(image_path, expected_sha256)
        local_size = self._get_file_size(image_path)
        local_size_mb = local_size / BYTES_PER_MEGABYTE
        logger.info(f"Local firmware size: {local_size_mb:.2f} MB")

        if self.skip_if_installed and expected_version:
            if self.check_if_installed(expected_version):
                logger.info("Firmware already installed, skipping flash")
                return

        try:
            ssh_driver = self.target.get_driver("SSHDriver")
            self.target.activate(ssh_driver)
        except Exception as error:
            raise StrategyError(f"Cannot upload firmware without SSH: {error}")

        self._check_tmp_space(ssh_driver, local_size)

        logger.info(f"Uploading firmware image to {self.remote_path}")
        ssh_driver.put(image_path, self.remote_path)

        self.verify_remote_integrity(ssh_driver, self.remote_path, local_sha256, local_size)

        if validate:
            logger.info("Validating firmware image compatibility")
            stdout, stderr, exit_code = ssh_driver.run(f"sysupgrade {VALIDATE_IMAGE_FLAG} {self.remote_path}")
            if exit_code != 0:
                stdout_text = "\n".join(stdout or [])
                stderr_text = "\n".join(stderr or [])
                raise StrategyError(f"Firmware validation failed:\n{stdout_text}\n{stderr_text}")
            logger.info("Firmware image validation passed")

        if validate_only:
            logger.info("Validation-only mode: all checks passed, skipping actual flash")
            return

        command_parts = ["sysupgrade"]

        if not keep_config:
            command_parts.append(KEEP_CONFIG_FLAG)

        if self.allow_downgrade:
            command_parts.append(FORCE_DOWNGRADE_FLAG)
            logger.info("Downgrade allowed: using -F flag")

        command_parts.append(self.remote_path)
        sysupgrade_command = " ".join(command_parts)

        logger.info(f"Executing sysupgrade command: {sysupgrade_command}")

        try:
            ssh_driver.run(sysupgrade_command, timeout=SYSUPGRADE_COMMAND_TIMEOUT)
        except Exception:
            pass

        logger.info(f"Waiting {self.verify_boot_timeout}s for device reboot")
        sleep(self.verify_boot_timeout)

    @step(title="verify_firmware", args=["expected_version"])
    def verify_version(self, expected_version: str = None):
        """
        Verifies the flashed firmware version.

        Args:
            expected_version: Expected version string (e.g., "2024.1")

        Raises:
            StrategyError: If version cannot be read or doesn't match expected
        """
        self.target.activate(self.shell)

        stdout, stderr, exit_code = self.shell.run(f"cat {OPENWRT_RELEASE_PATH}")
        if exit_code != 0:
            raise StrategyError("Failed to read firmware version")

        release_info = "\n".join(stdout)
        logger.info(f"Current firmware version:\n{release_info}")

        if expected_version:
            version_found = expected_version in release_info
            if not version_found:
                raise StrategyError(
                    f"Firmware version mismatch. Expected '{expected_version}' in:\n{release_info}"
                )
            logger.info(f"Firmware version verified: {expected_version}")
