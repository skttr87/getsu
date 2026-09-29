using System;
using System.IO;
using System.Runtime.InteropServices;

namespace AudioRestoreTool
{
    public enum EDataFlow { eRender = 0, eCapture = 1, eAll = 2 }
    public enum ERole { eConsole = 0, eMultimedia = 1, eCommunications = 2 }

    [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IMMDeviceEnumerator
    {
        int EnumAudioEndpoints(EDataFlow dataFlow, int dwStateMask, out IMMDeviceCollection ppDevices);
        int GetDefaultAudioEndpoint(EDataFlow dataFlow, ERole role, out IMMDevice ppEndpoint);
        int GetDevice(string pwstrId, out IMMDevice ppDevice);
        int RegisterEndpointNotificationCallback(IntPtr pClient);
        int UnregisterEndpointNotificationCallback(IntPtr pClient);
    }

    [Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IMMDeviceCollection
    {
        int GetCount(out int pcDevices);
        int Item(int nDevice, out IMMDevice ppDevice);
    }

    [Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IMMDevice
    {
        int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams, out IntPtr ppInterface);
        int OpenPropertyStore(int stgmAccess, out IPropertyStore ppProperties);
        int GetId([MarshalAs(UnmanagedType.LPWStr)] out string ppstrId);
        int GetState(out int pdwState);
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct PROPERTYKEY
    {
        public Guid fmtid;
        public int pid;
    }

    [StructLayout(LayoutKind.Explicit)]
    internal struct PROPVARIANT
    {
        [FieldOffset(0)] public short vt;
        [FieldOffset(8)] public IntPtr pwszVal;
    }

    [Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IPropertyStore
    {
        int GetCount(out int cProps);
        int GetAt(int iProp, out PROPERTYKEY pkey);
        int GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
        int SetValue(ref PROPERTYKEY key, ref PROPVARIANT propvar);
        int Commit();
    }

    [Guid("f8679f50-850a-41cf-9c72-430f290290c8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IPolicyConfigWin10
    {
        int GetMixFormat(string pszDeviceName, out IntPtr ppFormat);
        int GetDeviceFormat(string pszDeviceName, bool bDefault, out IntPtr ppFormat);
        int ResetDeviceFormat(string pszDeviceName);
        int SetDeviceFormat(string pszDeviceName, IntPtr pEndpointMode, IntPtr pFormat);
        int GetProcessingPeriod(string pszDeviceName, bool bDefault, out long pmftDefault, out long pmftMinimum);
        int SetProcessingPeriod(string pszDeviceName, long pmftPeriod);
        int GetShareMode(string pszDeviceName, out int pMode);
        int SetShareMode(string pszDeviceName, int mode);
        int GetPropertyValue(string pszDeviceName, IntPtr pKey, out IntPtr pPropVariant);
        int SetPropertyValue(string pszDeviceName, IntPtr pKey, IntPtr pPropVariant);
        int SetDefaultEndpoint(string pszDeviceName, ERole role);
        int SetEndpointVisibility(string pszDeviceName, bool bVisible);
    }

    [ComImport, Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9")]
    internal class PolicyConfigClientWin10 { }

    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    internal class MMDeviceEnumeratorComObject { }

    class Program
    {
        static readonly PROPERTYKEY PKEY_Device_FriendlyName = new PROPERTYKEY
        {
            fmtid = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0"),
            pid = 14
        };

        static string GetFriendlyName(IMMDevice dev)
        {
            try
            {
                IPropertyStore store;
                if (dev.OpenPropertyStore(0, out store) == 0 && store != null)
                {
                    PROPVARIANT pv;
                    PROPERTYKEY key = PKEY_Device_FriendlyName;
                    if (store.GetValue(ref key, out pv) == 0 && pv.pwszVal != IntPtr.Zero)
                    {
                        return Marshal.PtrToStringUni(pv.pwszVal);
                    }
                }
            }
            catch { }
            return "Unknown";
        }

        static bool SetAsDefault(string devId)
        {
            try
            {
                var policy = (IPolicyConfigWin10)new PolicyConfigClientWin10();
                policy.SetDefaultEndpoint(devId, ERole.eConsole);
                policy.SetDefaultEndpoint(devId, ERole.eMultimedia);
                policy.SetDefaultEndpoint(devId, ERole.eCommunications);
                return true;
            }
            catch (Exception ex)
            {
                Console.WriteLine("SetAsDefault error: " + ex.Message);
                return false;
            }
        }

        static int Main(string[] args)
        {
            if (args.Length == 0)
            {
                Console.WriteLine("Usage: AudioRestore.exe [--backup <file> | --restore <file> | --ensure-physical | --check-vbcable]");
                return 1;
            }

            var enumerator = (IMMDeviceEnumerator)new MMDeviceEnumeratorComObject();

            if (args[0] == "--check-vbcable")
            {
                // Returns 0 if active CABLE endpoint is found, 1 if not found
                IMMDeviceCollection col;
                if (enumerator.EnumAudioEndpoints(EDataFlow.eRender, 1 /* DEVICE_STATE_ACTIVE */, out col) == 0 && col != null)
                {
                    int count;
                    col.GetCount(out count);
                    for (int i = 0; i < count; i++)
                    {
                        IMMDevice dev;
                        if (col.Item(i, out dev) == 0 && dev != null)
                        {
                            string name = GetFriendlyName(dev).ToLower();
                            if (name.Contains("cable input") || (name.Contains("cable") && name.Contains("vb-audio")))
                            {
                                Console.WriteLine("VB-Cable is installed and active: " + name);
                                return 0; // Found
                            }
                        }
                    }
                }
                Console.WriteLine("VB-Cable is NOT installed");
                return 1; // Not found
            }
            else if (args[0] == "--backup" && args.Length > 1)
            {
                try
                {
                    IMMDevice dev;
                    if (enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender, ERole.eMultimedia, out dev) == 0 && dev != null)
                    {
                        string id;
                        dev.GetId(out id);
                        string name = GetFriendlyName(dev);
                        if (!name.ToLower().Contains("cable") && !name.ToLower().Contains("vb-audio"))
                        {
                            File.WriteAllText(args[1], id);
                            Console.WriteLine("Backed up physical default output: " + name + " (" + id + ")");
                            return 0;
                        }
                    }
                }
                catch (Exception ex)
                {
                    Console.WriteLine("Backup failed: " + ex.Message);
                    return 1;
                }
            }
            else if (args[0] == "--restore" && args.Length > 1)
            {
                string id = null;
                if (File.Exists(args[1]))
                {
                    id = File.ReadAllText(args[1]).Trim();
                }

                if (!string.IsNullOrEmpty(id))
                {
                    if (SetAsDefault(id))
                    {
                        Console.WriteLine("Successfully restored default audio endpoint to: " + id);
                        return 0;
                    }
                }

                return EnsurePhysical(enumerator);
            }
            else if (args[0] == "--ensure-physical")
            {
                return EnsurePhysical(enumerator);
            }

            return 0;
        }

        static int EnsurePhysical(IMMDeviceEnumerator enumerator)
        {
            try
            {
                IMMDevice curDev;
                if (enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender, ERole.eMultimedia, out curDev) == 0 && curDev != null)
                {
                    string curName = GetFriendlyName(curDev).ToLower();
                    if (!curName.Contains("cable") && !curName.Contains("vb-audio"))
                    {
                        Console.WriteLine("Current default output is already physical: " + curName);
                        return 0;
                    }
                }

                IMMDeviceCollection col;
                if (enumerator.EnumAudioEndpoints(EDataFlow.eRender, 1 /* DEVICE_STATE_ACTIVE */, out col) == 0 && col != null)
                {
                    int count;
                    col.GetCount(out count);
                    for (int i = 0; i < count; i++)
                    {
                        IMMDevice dev;
                        if (col.Item(i, out dev) == 0 && dev != null)
                        {
                            string name = GetFriendlyName(dev);
                            string nameLower = name.ToLower();
                            if (!nameLower.Contains("cable") && !nameLower.Contains("vb-audio"))
                            {
                                string id;
                                dev.GetId(out id);
                                if (SetAsDefault(id))
                                {
                                    Console.WriteLine("Set default output to physical device: " + name);
                                    return 0;
                                }
                            }
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine("EnsurePhysical error: " + ex.Message);
                return 1;
            }
            return 0;
        }
    }
}
