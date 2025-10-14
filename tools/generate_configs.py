#!/usr/bin/env python3
"""
YAML Configuration Generator for Hardware-in-the-Loop Testing
==============================================================

Generates Labgrid target YAML files from templates by injecting environment
variables and auto-detected paths.

Usage:
    python3 generate_configs.py                    # Generate all configs
    python3 generate_configs.py --target belkin1   # Generate specific config
    python3 generate_configs.py --dry-run          # Show what would be generated
"""

import os
import sys
import argparse
from pathlib import Path
from typing import Dict, Optional


class ConfigGenerator:
    """Generates Labgrid YAML configs from templates with environment variables."""
    
    def __init__(self, tests_dir: Optional[Path] = None):
        """
        Initialize the config generator.
        
        Args:
            tests_dir: Path to tests directory (auto-detected if not provided)
        """
        if tests_dir is None:
            tests_dir = Path(__file__).parent.parent
        
        self.tests_dir = Path(tests_dir).resolve()
        self.templates_dir = self.tests_dir / "targets" / "templates"
        self.output_dir = self.tests_dir / "targets"
        
        # Load environment variables with fallback defaults
        self.env = self._load_environment()
    
    def _load_environment(self) -> Dict[str, str]:
        """Load environment variables with intelligent defaults."""
        workspace_root = self.tests_dir.parent
        
        env = {
            # Paths
            'HIL_WORKSPACE_PATH': os.environ.get(
                'HIL_WORKSPACE_PATH',
                str(workspace_root)
            ),
            'HIL_UTILS_PATH': os.environ.get(
                'HIL_UTILS_PATH',
                str(workspace_root.parent / 'pi-hil-testing-utils')
            ),
            'HIL_IMAGES_PATH': os.environ.get(
                'HIL_IMAGES_PATH',
                str(workspace_root.parent / 'images')
            ),
            'HIL_TFTP_ROOT': os.environ.get(
                'HIL_TFTP_ROOT',
                '/srv/tftp'
            ),
            
            # Belkin RT3200 #1
            'HIL_BELKIN1_IP': os.environ.get('HIL_BELKIN1_IP', '192.168.20.182'),
            'HIL_BELKIN1_SERIAL': os.environ.get('HIL_BELKIN1_SERIAL', '/dev/belkin-rt3200-1'),
            'HIL_BELKIN1_RELAY_CHANNEL': os.environ.get('HIL_BELKIN1_RELAY_CHANNEL', '2'),
            
            # Belkin RT3200 #2
            'HIL_BELKIN2_IP': os.environ.get('HIL_BELKIN2_IP', '192.168.20.183'),
            'HIL_BELKIN2_SERIAL': os.environ.get('HIL_BELKIN2_SERIAL', '/dev/belkin-rt3200-2'),
            'HIL_BELKIN2_RELAY_CHANNEL': os.environ.get('HIL_BELKIN2_RELAY_CHANNEL', '3'),
            
            # GL-iNet MT300N-V2
            'HIL_GLINET_IP': os.environ.get('HIL_GLINET_IP', '192.168.20.181'),
            'HIL_GLINET_SERIAL': os.environ.get('HIL_GLINET_SERIAL', '/dev/glinet-mango'),
            'HIL_GLINET_RELAY_CHANNEL': os.environ.get('HIL_GLINET_RELAY_CHANNEL', '0'),
            'HIL_GLINET_SERIAL_ISOLATOR_CHANNEL': os.environ.get('HIL_GLINET_SERIAL_ISOLATOR_CHANNEL', '1'),
            
            # Arduino Relay
            'HIL_ARDUINO_RELAY_DEVICE': os.environ.get('HIL_ARDUINO_RELAY_DEVICE', '/dev/arduino-relay'),
            'HIL_ARDUINO_RELAY_BAUDRATE': os.environ.get('HIL_ARDUINO_RELAY_BAUDRATE', '115200'),
            
            # TFTP Server
            'HIL_TFTP_SERVER_IP': os.environ.get('HIL_TFTP_SERVER_IP', '192.168.20.234'),
        }
        
        return env
    
    def _expand_variables(self, content: str) -> str:
        """
        Expand ${VAR} and $VAR style variables in content.
        
        Args:
            content: String content with variables
            
        Returns:
            Content with expanded variables
        """
        result = content
        
        # Expand ${VAR} format
        for key, value in self.env.items():
            result = result.replace(f'${{{key}}}', value)
        
        # Expand $VAR format (but not $$ which is literal $)
        for key, value in self.env.items():
            # Simple word boundary replacement
            result = result.replace(f'${key}/', value + '/')
            result = result.replace(f'${key}\n', value + '\n')
            result = result.replace(f'${key} ', value + ' ')
        
        return result
    
    def generate_config(self, template_name: str, dry_run: bool = False) -> bool:
        """
        Generate a single config file from template.
        
        Args:
            template_name: Name of template file (e.g., 'belkin_rt3200_1.yaml.template')
            dry_run: If True, only show what would be generated
            
        Returns:
            True if successful, False otherwise
        """
        template_path = self.templates_dir / template_name
        
        if not template_path.exists():
            print(f"✗ Template not found: {template_path}", file=sys.stderr)
            return False
        
        # Read template
        try:
            with open(template_path, 'r') as f:
                template_content = f.read()
        except Exception as e:
            print(f"✗ Failed to read template {template_path}: {e}", file=sys.stderr)
            return False
        
        # Expand variables
        expanded_content = self._expand_variables(template_content)
        
        # Determine output filename (remove .template suffix)
        if template_name.endswith('.template'):
            output_name = template_name[:-9]  # Remove '.template'
        else:
            output_name = template_name
        
        output_path = self.output_dir / output_name
        
        if dry_run:
            print(f"Would generate: {output_path}")
            print("=" * 60)
            print(expanded_content)
            print("=" * 60)
            return True
        
        # Write output
        try:
            with open(output_path, 'w') as f:
                f.write(expanded_content)
            print(f"✓ Generated: {output_path}")
            return True
        except Exception as e:
            print(f"✗ Failed to write {output_path}: {e}", file=sys.stderr)
            return False
    
    def generate_all(self, dry_run: bool = False) -> int:
        """
        Generate all config files from templates.
        
        Args:
            dry_run: If True, only show what would be generated
            
        Returns:
            Number of configs successfully generated
        """
        if not self.templates_dir.exists():
            print(f"✗ Templates directory not found: {self.templates_dir}", file=sys.stderr)
            print("  Run this after setting up templates in targets/templates/", file=sys.stderr)
            return 0
        
        # Find all template files
        template_files = list(self.templates_dir.glob('*.yaml.template'))
        
        if not template_files:
            print(f"⚠ No template files found in {self.templates_dir}", file=sys.stderr)
            return 0
        
        print(f"Generating {len(template_files)} config(s) from templates...")
        print(f"Environment:")
        for key, value in self.env.items():
            exists = "✓" if Path(value).exists() else "✗"
            print(f"  {exists} {key}: {value}")
        print()
        
        success_count = 0
        for template_file in sorted(template_files):
            if self.generate_config(template_file.name, dry_run):
                success_count += 1
        
        print()
        print(f"Generated {success_count}/{len(template_files)} configs")
        
        return success_count


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Generate Labgrid YAML configs from templates',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 generate_configs.py                    # Generate all configs
  python3 generate_configs.py --dry-run          # Preview without writing
  python3 generate_configs.py --target belkin    # Generate belkin configs

Environment Variables:
  HIL_WORKSPACE_PATH  - Path to openwrt workspace
  HIL_UTILS_PATH      - Path to pi-hil-testing-utils
  HIL_IMAGES_PATH     - Path to firmware images
  HIL_TFTP_ROOT       - TFTP server root directory
        """
    )
    
    parser.add_argument(
        '--target',
        help='Generate specific target config (matches template name)',
        default=None
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be generated without writing files'
    )
    
    args = parser.parse_args()
    
    generator = ConfigGenerator()
    
    if args.target:
        # Generate specific target
        template_name = args.target
        if not template_name.endswith('.yaml.template'):
            template_name += '.yaml.template'
        
        success = generator.generate_config(template_name, args.dry_run)
        sys.exit(0 if success else 1)
    else:
        # Generate all targets
        success_count = generator.generate_all(args.dry_run)
        sys.exit(0 if success_count > 0 else 1)


if __name__ == '__main__':
    main()

