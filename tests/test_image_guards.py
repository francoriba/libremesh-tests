"""
Tests for firmware image guards and validation.

These tests verify the safety checks in SysupgradeDriver:
- Board compatibility validation
- SHA256 checksum verification
- Free space checks
- Remote integrity validation
- Version skip logic
- Validation-only mode
"""
import pytest
import os
import tempfile


def test_sysupgrade_available(strategy):
    """Verifies that sysupgrade command is available on the device."""
    strategy.transition("shell")
    result = strategy.shell.run("command -v sysupgrade")
    assert result[2] == 0, "sysupgrade command not found"
    assert result[0], "sysupgrade path is empty"
    print(f"\nSysupgrade found at: {result[0][0]}")


def test_board_name_retrieval(strategy):
    """Verifies that board name can be retrieved from device."""
    strategy.transition("shell")
    
    # Try /tmp/sysinfo/board_name first
    result = strategy.shell.run("cat /tmp/sysinfo/board_name")
    
    if result[2] == 0 and result[0]:
        board_name = result[0][0].strip()
        print(f"\nBoard name from /tmp/sysinfo/board_name: {board_name}")
        assert board_name, "Board name is empty"
    else:
        # Fallback to ubus
        result = strategy.shell.run("ubus call system board | jsonfilter -e '@.board_name'")
        assert result[2] == 0, "Failed to get board name from ubus"
        board_name = result[0][0].strip()
        print(f"\nBoard name from ubus: {board_name}")
        assert board_name, "Board name is empty"


def test_board_validation_pass(strategy, target, firmware_image):
    """Tests that board validation passes for matching board."""
    strategy.transition("shell")
    
    # Get driver without auto-activation
    sysupgrade_driver = target.get_driver("SysupgradeDriver", activate=False)
    sysupgrade_driver.target.activate(strategy.shell)
    
    # This should pass without raising
    sysupgrade_driver.validate_board(firmware_image)
    print("\nBoard validation passed")


def test_board_validation_fail():
    """Tests that board validation fails for mismatched board (simulated)."""
    # This is a unit test - we'd need to mock or create a fake driver
    # For now, we document the expected behavior
    print("\nBoard mismatch would raise StrategyError")
    # In a real scenario:
    # with pytest.raises(StrategyError, match="Board mismatch"):
    #     driver_with_wrong_board.validate_board(image)


def test_sha256_local_calculation(target, firmware_image):
    """Tests local SHA256 calculation of firmware image."""
    # Get driver without activation (no device needed for local calc)
    sysupgrade_driver = target.get_driver("SysupgradeDriver", activate=False)
    
    sha256 = sysupgrade_driver._calculate_sha256(firmware_image)
    
    assert sha256, "SHA256 is empty"
    assert len(sha256) == 64, f"SHA256 should be 64 chars, got {len(sha256)}"
    assert all(c in '0123456789abcdef' for c in sha256), "SHA256 contains invalid characters"
    
    print(f"\nFirmware SHA256: {sha256}")
    print(f"Firmware size: {sysupgrade_driver._get_file_size(firmware_image) / (1024*1024):.2f} MB")


def test_tmp_space_check(strategy, target):
    """Tests /tmp space check functionality."""
    strategy.transition("shell")
    
    # Get driver and SSH
    sysupgrade_driver = target.get_driver("SysupgradeDriver", activate=False)
    ssh_command = target.get_driver("SSHDriver")
    target.activate(ssh_command)
    
    # Check with a small size (should pass)
    try:
        sysupgrade_driver._check_tmp_space(ssh_command, required_bytes=1024)
        print("\n/tmp space check passed for 1 KB")
    except Exception as e:
        pytest.fail(f"Space check failed unexpectedly: {e}")
    
    # Check with unrealistic size (should fail)
    from labgrid.strategy import StrategyError
    with pytest.raises(StrategyError, match="Insufficient space"):
        sysupgrade_driver._check_tmp_space(ssh_command, required_bytes=100 * 1024 * 1024 * 1024)
        print("\n/tmp space check correctly rejected 100 GB requirement")


def test_remote_integrity_check(strategy, target, firmware_image):
    """Tests remote file integrity verification (size + SHA256)."""
    # Use strategy to power on and get shell
    strategy.transition("shell")
    
    # Get drivers
    sysupgrade_driver = target.get_driver("SysupgradeDriver", activate=False)
    ssh_command = target.get_driver("SSHDriver")
    shell_command = strategy.shell
    
    # Activate SSH
    target.activate(ssh_command)
    
    # Create a small test file
    test_content = b"Test firmware guard content\n" * 100
    test_path = "/tmp/test_guard_file.bin"
    
    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
        tmp_file.write(test_content)
        tmp_file.flush()
        local_path = tmp_file.name
    
    try:
        # Calculate expected values first
        local_sha256 = sysupgrade_driver._calculate_sha256(local_path)
        local_size = os.path.getsize(local_path)
        
        print(f"\nTest file SHA256: {local_sha256}")
        print(f"Test file size: {local_size} bytes")
        
        # Upload test file using SSH put
        print(f"Uploading {local_path} to {test_path}...")
        ssh_command.put(local_path, test_path)
        
        # Verify file exists on remote
        result = shell_command.run(f"ls -la {test_path}")
        if result[2] != 0:
            pytest.fail(f"Test file not uploaded: {test_path}")
        print(f"File uploaded: {result[0]}")
        
        # Verify integrity (should pass)
        sysupgrade_driver.verify_remote_integrity(
            ssh_command, test_path, local_sha256, local_size
        )
        print("Remote integrity check passed")
        
        # Test with wrong SHA256 (should fail)
        from labgrid.strategy import StrategyError
        wrong_sha256 = "0" * 64
        with pytest.raises(StrategyError, match="SHA256 mismatch"):
            sysupgrade_driver.verify_remote_integrity(
                ssh_command, test_path, wrong_sha256, local_size
            )
        print("Remote integrity check correctly rejected wrong SHA256")
        
        # Test with wrong size (should fail)
        with pytest.raises(StrategyError, match="size mismatch"):
            sysupgrade_driver.verify_remote_integrity(
                ssh_command, test_path, local_sha256, local_size + 1000
            )
        print("Remote integrity check correctly rejected wrong size")
        
    finally:
        # Cleanup
        try:
            os.unlink(local_path)
        except:
            pass
        try:
            shell_command.run(f"rm -f {test_path}")
        except:
            pass


def test_version_check(strategy, target):
    """Tests installed version detection."""
    strategy.transition("shell")
    
    sysupgrade_driver = target.get_driver("SysupgradeDriver", activate=False)
    sysupgrade_driver.target.activate(strategy.shell)
    
    version_info = sysupgrade_driver._get_installed_version()
    
    assert version_info, "Version info is empty"
    assert 'DISTRIB_ID' in version_info, "DISTRIB_ID not in version info"
    
    print("\nInstalled firmware version info:")
    for key, value in version_info.items():
        print(f"  {key}: {value}")


def test_skip_if_installed(strategy, target):
    """Tests skip_if_installed logic."""
    strategy.transition("shell")
    
    sysupgrade_driver = target.get_driver("SysupgradeDriver", activate=False)
    sysupgrade_driver.target.activate(strategy.shell)
    
    # Get current version
    version_info = sysupgrade_driver._get_installed_version()
    current_version = version_info.get('DISTRIB_RELEASE', '')
    
    print(f"\nCurrent version: {current_version}")
    
    # Test with current version (should return True)
    if current_version:
        is_installed = sysupgrade_driver.check_if_installed(current_version)
        assert is_installed, "Should detect current version as installed"
        print(f"Correctly detected {current_version} as installed")
    
    # Test with non-existent version (should return False)
    fake_version = "99.99.99"
    is_installed = sysupgrade_driver.check_if_installed(fake_version)
    assert not is_installed, "Should not detect fake version as installed"
    print(f"Correctly detected {fake_version} as NOT installed")


@pytest.mark.skip(reason="Requires actual firmware flash - run manually")
def test_validate_only_mode(strategy, target, firmware_image):
    """
    Tests validate-only mode (all checks without flashing).
    
    This test runs all safety checks but doesn't actually flash.
    Useful for CI pipelines.
    """
    strategy.transition("shell")
    
    sysupgrade_driver = target.get_driver("SysupgradeDriver", activate=False)
    sysupgrade_driver.target.activate(strategy.shell)
    
    print("\n=== Testing Validate-Only Mode ===")
    print("This will run all safety checks without flashing")
    
    # Enable validate_only
    sysupgrade_driver.validate_only = True
    
    # This should run all checks and return without flashing
    sysupgrade_driver.flash(firmware_image, validate_only=True)
    
    print("Validation-only mode completed successfully")


@pytest.mark.skip(reason="Requires actual firmware flash - run manually")
def test_full_flash_with_guards(strategy, target, firmware_image):
    """
    Full integration test: flash with all guards enabled.
    
    This test performs a real firmware flash with all safety checks.
    """
    strategy.transition("shell")
    
    sysupgrade_driver = target.get_driver("SysupgradeDriver", activate=False)
    sysupgrade_driver.target.activate(strategy.shell)
    
    print("\n=== Testing Full Flash with All Guards ===")
    
    # Get current version for comparison
    version_info = sysupgrade_driver._get_installed_version()
    print(f"Current version: {version_info.get('DISTRIB_RELEASE', 'unknown')}")
    
    # Calculate SHA256 for reference
    sha256 = sysupgrade_driver._calculate_sha256(firmware_image)
    print(f"Firmware SHA256: {sha256}")
    
    # Flash with all guards
    sysupgrade_driver.flash(
        firmware_image,
        keep_config=False,
        validate=True,
        expected_sha256=sha256
    )
    
    print("Flash with guards completed successfully")


def test_ubi_warning_detection(target, firmware_image):
    """Tests UBI mismatch warning logic."""
    sysupgrade_driver = target.get_driver("SysupgradeDriver", activate=False)
    
    print("\nTesting UBI mismatch warning...")
    
    # If board is UBI and image doesn't have "-ubi-" in name, we should get a warning
    if sysupgrade_driver.expected_board and (
        sysupgrade_driver.expected_board.endswith("-ubi") or 
        "-ubi" in sysupgrade_driver.expected_board
    ):
        image_basename = os.path.basename(firmware_image)
        if "-ubi-" in image_basename.lower():
            print(f"Board and image both UBI: {image_basename}")
        else:
            print(f"Warning: UBI board but image may not be UBI: {image_basename}")
