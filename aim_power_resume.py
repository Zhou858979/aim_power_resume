import importlib.util, sys, os, glob
_d = os.path.dirname(os.path.abspath(__file__))
_so = glob.glob(os.path.join(_d, 'aim_power_resume*.so'))
if not _so: raise ImportError("aim_power_resume .so not found")
_spec = importlib.util.spec_from_file_location('aim_power_resume_impl', _so[0])
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
sys.modules[__name__] = _mod