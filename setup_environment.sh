#!/bin/bash
# Hardware-in-the-Loop Testing Environment Setup
# ==============================================
# Auto-detects paths and configures the testing environment
#
# Usage:
#   ./setup_environment.sh              # Setup and generate configs
#   source ./setup_environment.sh       # Setup environment in current shell
#   ./setup_environment.sh --check      # Only check configuration
#   ./setup_environment.sh --generate   # Only generate YAML configs
#   ./setup_environment.sh --clean      # Remove all generated files

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Determine script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "================================================"
echo "  Hardware-in-the-Loop Environment Setup"
echo "================================================"
echo ""

# Auto-detect paths with fallbacks
detect_hil_utils_path() {
    local candidates=(
        "$WORKSPACE_ROOT/../pi-hil-testing-utils"
        "$HOME/pi/pi-hil-testing-utils"
        "/opt/pi-hil-testing-utils"
    )
    
    for path in "${candidates[@]}"; do
        if [ -d "$path" ]; then
            echo "$path"
            return 0
        fi
    done
    
    echo "${candidates[0]}"  # Return first candidate as fallback
    return 1
}

detect_images_path() {
    local candidates=(
        "$WORKSPACE_ROOT/../pi-hil-testing-utils/firmwares"
        "$HOME/pi/pi-hil-testing-utils/firmwares"
        "/opt/openwrt-images"
    )
    
    for path in "${candidates[@]}"; do
        if [ -d "$path" ]; then
            echo "$path"
            return 0
        fi
    done
    
    echo "${candidates[0]}"  # Return first candidate as fallback
    return 1
}

# Detect paths (use existing env vars if set)
export HIL_WORKSPACE_PATH="${HIL_WORKSPACE_PATH:-$WORKSPACE_ROOT}"

if [ -z "$HIL_UTILS_PATH" ]; then
    HIL_UTILS_PATH=$(detect_hil_utils_path)
    export HIL_UTILS_PATH
fi

if [ -z "$HIL_IMAGES_PATH" ]; then
    HIL_IMAGES_PATH=$(detect_images_path)
    export HIL_IMAGES_PATH
fi

export HIL_TFTP_ROOT="${HIL_TFTP_ROOT:-/srv/tftp}"

# Validate paths
VALIDATION_ERRORS=0

echo "Configuration:"
echo "  Workspace: $HIL_WORKSPACE_PATH"

if [ -d "$HIL_UTILS_PATH" ]; then
    echo -e "  ${GREEN}✓${NC} Utils:     $HIL_UTILS_PATH"
else
    echo -e "  ${RED}✗${NC} Utils:     $HIL_UTILS_PATH (NOT FOUND)"
    VALIDATION_ERRORS=$((VALIDATION_ERRORS + 1))
fi

if [ -d "$HIL_IMAGES_PATH" ]; then
    echo -e "  ${GREEN}✓${NC} Images:    $HIL_IMAGES_PATH"
else
    echo -e "  ${YELLOW}⚠${NC} Images:    $HIL_IMAGES_PATH (NOT FOUND - OK if not flashing)"
fi

if [ -d "$HIL_TFTP_ROOT" ]; then
    echo -e "  ${GREEN}✓${NC} TFTP:      $HIL_TFTP_ROOT"
else
    echo -e "  ${YELLOW}⚠${NC} TFTP:      $HIL_TFTP_ROOT (NOT FOUND - OK if not using U-Boot recovery)"
fi

echo ""

# Clean mode - remove all generated files
if [ "$1" = "--clean" ]; then
    echo "Cleaning generated configuration files..."
    echo ""
    
    # Remove generated YAMLs (but keep templates)
    GENERATED_YAMLS=(
        "targets/belkin_rt3200_1.yaml"
        "targets/belkin_rt3200_2.yaml"
        "targets/gl-mt300n-v2.yaml"
        "targets/mesh_testbed.yaml"
    )
    
    CLEANED_COUNT=0
    for yaml in "${GENERATED_YAMLS[@]}"; do
        if [ -f "$SCRIPT_DIR/$yaml" ]; then
            rm "$SCRIPT_DIR/$yaml"
            echo -e "  ${GREEN}✓${NC} Removed: $yaml"
            CLEANED_COUNT=$((CLEANED_COUNT + 1))
        fi
    done
    
    # Remove .envrc if it exists (but keep envrc.template)
    if [ -f "$SCRIPT_DIR/.envrc" ]; then
        rm "$SCRIPT_DIR/.envrc"
        echo -e "  ${GREEN}✓${NC} Removed: .envrc"
        CLEANED_COUNT=$((CLEANED_COUNT + 1))
    fi
    
    echo ""
    if [ $CLEANED_COUNT -gt 0 ]; then
        echo -e "${GREEN}✓ Cleaned $CLEANED_COUNT generated file(s)${NC}"
        echo ""
        echo "To regenerate configurations, run:"
        echo "  ./setup_environment.sh"
    else
        echo -e "${YELLOW}⚠ No generated files found to clean${NC}"
    fi
    
    exit 0
fi

# Check-only mode
if [ "$1" = "--check" ]; then
    if [ $VALIDATION_ERRORS -eq 0 ]; then
        echo -e "${GREEN}✓ Configuration valid${NC}"
        exit 0
    else
        echo -e "${RED}✗ Configuration has errors${NC}"
        exit 1
    fi
fi

# Generate YAML configs
if [ "$1" != "--skip-generate" ]; then
    echo "Generating YAML configurations from templates..."
    if [ -f "$SCRIPT_DIR/tools/generate_configs.py" ]; then
        python3 "$SCRIPT_DIR/tools/generate_configs.py"
        if [ $? -eq 0 ]; then
            echo -e "${GREEN}✓ YAML configs generated${NC}"
        else
            echo -e "${RED}✗ Failed to generate YAML configs${NC}"
            exit 1
        fi
    else
        echo -e "${YELLOW}⚠ Config generator not found, skipping${NC}"
    fi
    echo ""
fi

# Save to .envrc if it doesn't exist
if [ ! -f "$SCRIPT_DIR/.envrc" ] && [ -f "$SCRIPT_DIR/envrc.template" ]; then
    echo "Creating .envrc from template..."
    cp "$SCRIPT_DIR/envrc.template" "$SCRIPT_DIR/.envrc"
    echo -e "${GREEN}✓ Created .envrc${NC}"
    echo "  Tip: Install direnv for automatic environment loading"
    echo ""
fi

# Final status
if [ $VALIDATION_ERRORS -eq 0 ]; then
    echo -e "${GREEN}✓ Environment setup complete!${NC}"
    echo ""
    echo "Next steps:"
    echo "  1. Source environment: source $SCRIPT_DIR/envrc.template"
    echo "  2. Or use direnv: cd $SCRIPT_DIR && direnv allow"
    echo "  3. Run tests: make tests/mesh_testbed K=test_mesh_basic_connectivity"
    echo ""
    exit 0
else
    echo -e "${YELLOW}⚠ Environment setup complete with warnings${NC}"
    echo ""
    echo "To fix missing paths, set environment variables:"
    echo "  export HIL_UTILS_PATH=/path/to/pi-hil-testing-utils"
    echo "  export HIL_IMAGES_PATH=/path/to/images"
    echo ""
    exit 0
fi

