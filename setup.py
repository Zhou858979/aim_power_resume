from setuptools import setup
from Cython.Build import cythonize

setup(
    ext_modules=cythonize([
        "aim_power_resume.py"
    ])
)