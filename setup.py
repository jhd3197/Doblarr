"""Include application assets without duplicating the development web directory."""

from pathlib import Path
from shutil import copy2, copytree

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent
        target = Path(self.build_lib) / "doblarr"
        copytree(root / "web", target / "web", dirs_exist_ok=True)
        copy2(root / "config.example.yaml", target / "config.example.yaml")


setup(cmdclass={"build_py": BuildPy})
