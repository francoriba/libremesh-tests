"""
Tests for U-Boot recovery functionality.
"""
import pytest


# comment to enable tests
# @pytest.mark.skip(reason="Manual test - requires intentionally bricking device")
def test_uboot_recovery_manual(strategy, target, firmware_image):
    """
    Manual test for U-Boot recovery.
    
    This test power cycles and boots via U-Boot/TFTP.
    """
    print("\n=== Manual U-Boot Recovery Test ===")
    print("This will power cycle and boot via U-Boot/TFTP")
    
    # Attempt recovery directly (this handles power on and U-Boot activation)
    strategy.attempt_uboot_recovery(firmware_image)
    
    # Verify we can get shell after recovery
    strategy.transition("shell")
    strategy.configure_libremesh_network()  # Re-configurar red post-recovery
    
    result = strategy.shell.run("echo recovery_test")
    assert result[2] == 0
    
    # Verify SSH is also working
    try:
        ssh = target.get_driver("SSHDriver", activate=False)
        
        # Deactivate stale SSH connection if active (ignore errors if not active)
        try:
            target.deactivate(ssh)
        except Exception:
            pass
        
        # Activate SSH with fresh connection
        target.activate(ssh)
        ssh.run_check("true")
        print("SSH connection successful after recovery")
    except Exception as e:
        pytest.fail(f"SSH failed after recovery: {e}")
    
    print("U-Boot recovery successful!")


def test_uboot_config_present(env):
    """Verifies that U-Boot driver is properly configured in YAML."""
    target = env.get_target()
    
    # Check if UBootDriver is in the target's drivers (without activating it)
    driver_classes = [type(d).__name__ for d in target.drivers]
    
    if "UBootDriver" in driver_classes:
        print(f"\n✓ U-Boot driver is configured in target YAML")
        
        # Get the driver instance (without activating)
        for driver in target.drivers:
            if type(driver).__name__ == "UBootDriver":
                uboot = driver
                print(f"  Prompt: {uboot.prompt}")
                print(f"  Autoboot message: {getattr(uboot, 'autoboot', 'N/A')}")
                print(f"  Interrupt key: {repr(getattr(uboot, 'interrupt', 'N/A'))}")
                break
    else:
        pytest.skip("U-Boot driver not configured in target YAML")


def test_tftp_config_present(env):
    """Verifies that TFTP is properly configured for recovery."""
    target = env.get_target()
    
    # Check if PhysicalDeviceStrategy has tftp_root configured
    driver_classes = [type(d).__name__ for d in target.drivers]
    
    if "PhysicalDeviceStrategy" in driver_classes:
        for driver in target.drivers:
            if type(driver).__name__ == "PhysicalDeviceStrategy":
                strategy = driver
                tftp_root = getattr(strategy, 'tftp_root', None)
                
                if tftp_root:
                    import os
                    print(f"\n✓ TFTP root configured: {tftp_root}")
                    if os.path.exists(tftp_root):
                        print(f"  ✓ TFTP directory exists")
                    else:
                        pytest.skip(f"TFTP directory does not exist: {tftp_root}")
                else:
                    pytest.skip("TFTP root not configured in PhysicalDeviceStrategy")
                break
    else:
        pytest.skip("PhysicalDeviceStrategy not configured")


def test_recovery_strategy_config(env):
    """Verifies that PhysicalDeviceStrategy has recovery enabled."""
    target = env.get_target()
    
    # Check if PhysicalDeviceStrategy is configured
    driver_classes = [type(d).__name__ for d in target.drivers]
    
    if "PhysicalDeviceStrategy" in driver_classes:
        print(f"\n✓ PhysicalDeviceStrategy is configured")
        
        # Get the strategy instance
        for driver in target.drivers:
            if type(driver).__name__ == "PhysicalDeviceStrategy":
                strategy = driver
                enable_recovery = getattr(strategy, 'enable_uboot_recovery', False)
                max_attempts = getattr(strategy, 'max_recovery_attempts', 0)
                
                print(f"  U-Boot recovery enabled: {enable_recovery}")
                print(f"  Max recovery attempts: {max_attempts}")
                
                if not enable_recovery:
                    pytest.skip("U-Boot recovery is not enabled in PhysicalDeviceStrategy")
                break
    else:
        pytest.skip("PhysicalDeviceStrategy not configured in target YAML")
