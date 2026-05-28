#!/usr/bin/env python3
"""
Setup and Validation Script for Mixed-Precision FFT Optimization
ASIC version: checks Cadence Genus instead of Xilinx Vivado.
All other checks (Python version, packages, simulator, Verilog sources,
configuration) are unchanged.
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path

class SetupValidator:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.checks_passed = 0
        self.checks_failed = 0

    # ------------------------------------------------------------------
    # Unchanged checks
    # ------------------------------------------------------------------
    def check_python_version(self):
        """Check Python version"""
        print("Checking Python version...")
        version = sys.version_info
        if version.major >= 3 and version.minor >= 8:
            print(f"  ✓ Python {version.major}.{version.minor}.{version.micro}")
            self.checks_passed += 1
            return True
        else:
            print(f"  ✗ Python {version.major}.{version.minor}.{version.micro}")
            self.errors.append(
                f"Python 3.8+ required, found {version.major}.{version.minor}"
            )
            self.checks_failed += 1
            return False

    def check_python_packages(self):
        """Check required Python packages"""
        print("\nChecking Python packages:")

        required_packages = {
            'numpy':      'numpy',
            'pymoo':      'pymoo',
            'matplotlib': 'matplotlib',
            'scipy':      'scipy'
        }

        all_present = True
        for display_name, import_name in required_packages.items():
            try:
                __import__(import_name)
                print(f"  ✓ {display_name}")
                self.checks_passed += 1
            except ImportError:
                print(f"  ✗ {display_name} - NOT FOUND")
                self.errors.append(f"Missing package: {display_name}")
                all_present = False
                self.checks_failed += 1

        return all_present

    # ------------------------------------------------------------------
    # CHANGED: check_genus replaces check_vivado
    # ------------------------------------------------------------------
    def check_genus(self):
        """
        Check Cadence Genus installation.
        Reads GENUS_PATH and LIBERTY_LIB_PATH from globalVariablesMixedFFT.py
        and verifies both exist on disk.
        """
        print("\nChecking Cadence Genus installation:")

        if not os.path.exists('globalVariablesMixedFFT.py'):
            print("  ✗ globalVariablesMixedFFT.py not found")
            self.errors.append("Configuration file missing")
            self.checks_failed += 1
            return False

        try:
            from globalVariablesMixedFFT import GENUS_PATH, LIBERTY_LIB_PATH
        except ImportError as e:
            print(f"  ✗ Could not import configuration: {e}")
            self.errors.append("Configuration file import error")
            self.checks_failed += 1
            return False

        # --- Check Genus executable ---
        if os.path.exists(GENUS_PATH):
            print(f"  ✓ Genus executable found at: {GENUS_PATH}")
            self.checks_passed += 1

            # Try to get version by invoking genus -version
            try:
                result = subprocess.run(
                    [GENUS_PATH, '-version'],
                    capture_output=True,
                    text=True,
                    timeout=15
                )
                # Genus prints version info to stdout even with -version flag
                version_output = (result.stdout + result.stderr).strip()
                version_lines = [
                    l for l in version_output.splitlines()
                    if 'genus' in l.lower() or 'version' in l.lower() or 'release' in l.lower()
                ]
                if version_lines:
                    print(f"  ✓ {version_lines[0].strip()}")
                    self.checks_passed += 1
                else:
                    print(f"  ⚠ Could not determine Genus version (executable ran but gave no version string)")
                    self.warnings.append("Genus version string not parsed — verify manually")
            except subprocess.TimeoutExpired:
                print(f"  ⚠ Genus version check timed out")
                self.warnings.append("Genus -version timed out")
            except Exception as e:
                print(f"  ⚠ Could not check Genus version: {e}")
                self.warnings.append("Genus version check failed")
        else:
            print(f"  ✗ Genus executable not found at: {GENUS_PATH}")
            self.errors.append(
                "Update GENUS_PATH in globalVariablesMixedFFT.py to point "
                "to your Cadence Genus installation"
            )
            self.checks_failed += 1
            return False

        # --- Check Liberty library ---
        print("\nChecking Liberty standard-cell library:")
        if os.path.exists(LIBERTY_LIB_PATH):
            print(f"  ✓ Liberty .lib found at: {LIBERTY_LIB_PATH}")
            self.checks_passed += 1
        else:
            print(f"  ✗ Liberty .lib not found at: {LIBERTY_LIB_PATH}")
            self.errors.append(
                "Update LIBERTY_LIB_PATH in globalVariablesMixedFFT.py "
                "to point to your PDK Liberty file (.lib)"
            )
            self.checks_failed += 1
            return False

        # --- Check genus_synthesis.tcl exists ---
        print("\nChecking Genus TCL synthesis script:")
        if os.path.exists('./genus_synthesis.tcl'):
            print("  ✓ genus_synthesis.tcl found")
            self.checks_passed += 1
        else:
            print("  ✗ genus_synthesis.tcl not found in current directory")
            self.errors.append(
                "genus_synthesis.tcl is missing — place it in the same "
                "directory as the Python scripts"
            )
            self.checks_failed += 1
            return False

        return True

    # ------------------------------------------------------------------
    # Unchanged checks
    # ------------------------------------------------------------------
    def check_simulator(self):
        """Check for iverilog and vvp"""
        print("\nChecking Verilog simulator (iverilog/vvp):")
        import shutil
        all_present = True
        
        for sim in ['iverilog', 'vvp']:
            if shutil.which(sim):
                print(f"  ✓ {sim} found")
                self.checks_passed += 1
            else:
                print(f"  ✗ {sim} NOT FOUND")
                self.errors.append(f"Simulator missing: {sim}. Please ensure Icarus Verilog is on your PATH.")
                all_present = False
                self.checks_failed += 1

        return all_present

    def check_verilog_sources(self):
        """
        Check Verilog source files.
        If directory exists, checks files.
        If files missing locally, copies them from uploads directory.
        """
        print("\nChecking and Populating Verilog source files:")

        required_files = [
            'adder.v',
            'multiplier.v',
            'twiddle_rom.v',
            'agu.v',
            'memory.v',
            'butterfly.v'
        ]

        upload_dir = Path('/mnt/user-data/uploads')
        dest_dir   = Path('./verilog_sources')

        if not dest_dir.exists():
            print(f"  ℹ Creating directory: {dest_dir}")
            dest_dir.mkdir(parents=True, exist_ok=True)
            self.checks_passed += 1

        all_present = True

        for fname in required_files:
            dest_file = dest_dir / fname
            src_file  = upload_dir / fname

            if dest_file.exists():
                print(f"  ✓ {fname} (found locally)")
                self.checks_passed += 1
            elif src_file.exists():
                try:
                    shutil.copy2(src_file, dest_file)
                    print(f"  ✓ {fname} (copied from uploads)")
                    self.checks_passed += 1
                except Exception as e:
                    print(f"  ✗ {fname} - Failed to copy: {e}")
                    self.errors.append(f"Failed to copy {fname}")
                    all_present = False
                    self.checks_failed += 1
            else:
                print(f"  ✗ {fname} - NOT FOUND in {dest_dir} or {upload_dir}")
                self.errors.append(f"Missing Verilog file: {fname}")
                all_present = False
                self.checks_failed += 1

        return all_present

    def create_directories(self):
        """Create necessary working directories"""
        print("\nCreating directory structure:")

        directories = [
            './generated_designs',
            './genus_work',        # replaces vivado_projects
            './reports',
            './sim',
            './results'
        ]

        for directory in directories:
            os.makedirs(directory, exist_ok=True)
            print(f"  ✓ {directory}")
            self.checks_passed += 1

        return True

    def validate_configuration(self):
        """Validate configuration parameters"""
        print("\nValidating configuration:")

        try:
            if not os.path.exists('globalVariablesMixedFFT.py'):
                return False

            from globalVariablesMixedFFT import (
                POPULATION, GENERATIONS, FFT_SIZES,
                CLOCK_PERIOD, ASIC_PROCESS,
                MAX_POWER_MW, MAX_AREA_UM2
            )

            if 0 < POPULATION <= 100:
                print(f"  ✓ Population size: {POPULATION}")
                self.checks_passed += 1
            else:
                print(f"  ⚠ Population size unusual: {POPULATION}")
                self.warnings.append("Check POPULATION value")

            if 0 < GENERATIONS <= 1000:
                print(f"  ✓ Generations: {GENERATIONS}")
                self.checks_passed += 1
            else:
                print(f"  ⚠ Generations unusual: {GENERATIONS}")
                self.warnings.append("Check GENERATIONS value")

            print(f"  ✓ FFT sizes       : {FFT_SIZES}")
            print(f"  ✓ Clock period    : {CLOCK_PERIOD} ns")
            print(f"  ✓ ASIC process    : {ASIC_PROCESS}")
            print(f"  ✓ Max power       : {MAX_POWER_MW} mW")
            print(f"  ✓ Max area        : {MAX_AREA_UM2} µm²")
            self.checks_passed += 5

            return True

        except ImportError as e:
            print(f"  ✗ Configuration error: {e}")
            self.errors.append("Could not validate configuration")
            self.checks_failed += 1
            return False

    # ------------------------------------------------------------------
    # Master runner
    # ------------------------------------------------------------------
    def run_all_checks(self):
        """Run all validation checks"""
        print("=" * 60)
        print("Mixed-Precision FFT Optimization - Setup Validation")
        print("(ASIC mode: Cadence Genus)")
        print("=" * 60)

        self.check_python_version()
        self.check_python_packages()
        self.check_genus()          # replaces check_vivado()
        self.check_simulator()
        self.validate_configuration()

        print("\n" + "=" * 60)
        print("Validation Summary")
        print("=" * 60)
        print(f"Checks passed: {self.checks_passed}")
        print(f"Checks failed: {self.checks_failed}")
        print(f"Warnings     : {len(self.warnings)}")

        if self.errors:
            print("\n❌ ERRORS:")
            for i, error in enumerate(self.errors, 1):
                print(f"  {i}. {error}")

        if self.warnings:
            print("\n⚠️  WARNINGS:")
            for i, warning in enumerate(self.warnings, 1):
                print(f"  {i}. {warning}")

        if not self.errors:
            print("\n✅ Setup validation PASSED!")
            print("\nYou can now run the optimization:")
            print("  python runMixedFFTOptimization.py --mode test")
            return True
        else:
            print("\n❌ Setup validation FAILED!")
            print("\nPlease fix the errors above before running optimization.")
            return False


def install_missing_packages():
    """Attempt to install missing Python packages"""
    print("\nAttempting to install missing packages...")

    packages = ['numpy', 'pymoo', 'matplotlib', 'scipy']

    for package in packages:
        try:
            __import__(package)
        except ImportError:
            print(f"Installing {package}...")
            subprocess.check_call([
                sys.executable, '-m', 'pip', 'install', package
            ])


def main():
    """Main setup function"""
    import argparse

    parser = argparse.ArgumentParser(
        description='Setup and validate Mixed-Precision FFT Optimization (ASIC/Genus)'
    )
    parser.add_argument(
        '--install-packages',
        action='store_true',
        help='Attempt to install missing Python packages'
    )

    args = parser.parse_args()

    if args.install_packages:
        install_missing_packages()

    validator = SetupValidator()
    success = validator.run_all_checks()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
