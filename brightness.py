"""Internal laptop screen brightness control via WMI.

Uses comtypes (same COM library pycaw/volume.py uses) rather than pywin32's
wmi/pythoncom. Mixing pythoncom.CoInitialize() and comtypes.CoInitialize()
on the same thread was found to silently break WmiSetBrightness calls (the
same call worked fine standalone, and worked via PowerShell's CIM cmdlets,
but failed as soon as a comtypes-based object like Volume also existed on
the same thread) - staying on one COM library for both avoids that.
"""
import comtypes.client


class Brightness:
    def __init__(self):
        comtypes.CoInitialize()
        locator = comtypes.client.CreateObject("WbemScripting.SWbemLocator")
        self._service = locator.ConnectServer(".", "root\\wmi")
        if self._service.InstancesOf("WmiMonitorBrightness").Count == 0:
            raise RuntimeError("WMI brightness control is not available on this device")

    def get(self) -> float:
        """Current brightness as a 0.0-1.0 fraction."""
        instances = self._service.InstancesOf("WmiMonitorBrightness")
        for i in range(instances.Count):
            inst = instances.ItemIndex(i)
            return inst.Properties_("CurrentBrightness").Value / 100.0
        return 0.5

    def set(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, fraction))
        value = int(round(fraction * 100))
        instances = self._service.InstancesOf("WmiMonitorBrightnessMethods")
        for i in range(instances.Count):
            inst = instances.ItemIndex(i)
            method = inst.Methods_("WmiSetBrightness")
            in_params = method.InParameters.SpawnInstance_()
            in_params.Properties_("Timeout").Value = 1
            in_params.Properties_("Brightness").Value = value
            inst.ExecMethod_("WmiSetBrightness", in_params)
            return
