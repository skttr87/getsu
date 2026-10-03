using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;

[assembly: AssemblyTitle("Getsu Audio Endpoint Helper")]
[assembly: AssemblyDescription("Preserves and restores physical speaker endpoints during driver installation.")]
[assembly: AssemblyCompany("Getsu AI")]
[assembly: AssemblyProduct("Getsu AI Noise Cancellation")]
[assembly: AssemblyCopyright("Copyright (c) 2026 Ihsan (@skttr87)")]
[assembly: AssemblyVersion("1.2.0.0")]
[assembly: AssemblyFileVersion("1.2.0.0")]

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
        [PreserveSig] int GetMixFormat(string pszDeviceName, IntPtr ppFormat);
        [PreserveSig] int GetDeviceFormat(string pszDeviceName, bool bDefault, IntPtr ppFormat);
        [PreserveSig] int ResetDeviceFormat(string pszDeviceName);
        [PreserveSig] int SetDeviceFormat(string pszDeviceName, IntPtr pEndpointMode, IntPtr pFormat);
        [PreserveSig] int GetProcessingPeriod(string pszDeviceName, bool bDefault, IntPtr pmftDefault, IntPtr pmftMinimum);
        [PreserveSig] int SetProcessingPeriod(string pszDeviceName, IntPtr pmftPeriod);
        [PreserveSig] int GetShareMode(string pszDeviceName, IntPtr pMode);
        [PreserveSig] int SetShareMode(string pszDeviceName, IntPtr mode);
        [PreserveSig] int GetPropertyValue(string pszDeviceName, bool bFxStore, IntPtr pKey, IntPtr pPropVariant);
        [PreserveSig] int SetPropertyValue(string pszDeviceName, bool bFxStore, IntPtr pKey, IntPtr pPropVariant);
        [PreserveSig] int SetDefaultEndpoint(string pszDeviceName, ERole role);
        [PreserveSig] int SetEndpointVisibility(string pszDeviceName, bool bVisible);
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
                int hr1 = policy.SetDefaultEndpoint(devId, ERole.eConsole);
                int hr2 = policy.SetDefaultEndpoint(devId, ERole.eMultimedia);
                int hr3 = policy.SetDefaultEndpoint(devId, ERole.eCommunications);
                if (hr1 == 0 || hr2 == 0 || hr3 == 0)
                    return true;
                Console.WriteLine(string.Format("SetDefaultEndpoint HR: 0x{0:X8}, 0x{1:X8}, 0x{2:X8}", hr1, hr2, hr3));
                return false;
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
                Console.WriteLine("Usage: AudioRestore.exe [--backup <file> | --restore <file> | --ensure-physical | --check-vbcable | --get-default-capture | --get-cable-capture | --set-default-capture <id> | --restore-capture <id|file> | --ensure-physical-capture]");
                return 1;
            }

            var enumerator = (IMMDeviceEnumerator)new MMDeviceEnumeratorComObject();

            if (args[0] == "--get-default-capture")
            {
                try
                {
                    IMMDevice dev = null;
                    if (enumerator.GetDefaultAudioEndpoint(EDataFlow.eCapture, ERole.eConsole, out dev) != 0 || dev == null)
                    {
                        enumerator.GetDefaultAudioEndpoint(EDataFlow.eCapture, ERole.eCommunications, out dev);
                    }
                    if (dev != null)
                    {
                        string id;
                        dev.GetId(out id);
                        string name = GetFriendlyName(dev);
                        Console.WriteLine(id + "|" + name);
                        return 0;
                    }
                }
                catch (Exception ex)
                {
                    Console.WriteLine("GetDefaultCapture error: " + ex.Message);
                    return 1;
                }
                return 1;
            }
            else if (args[0] == "--get-cable-capture")
            {
                try
                {
                    IMMDeviceCollection col;
                    if (enumerator.EnumAudioEndpoints(EDataFlow.eCapture, 1 /* DEVICE_STATE_ACTIVE */, out col) == 0 && col != null)
                    {
                        int count;
                        col.GetCount(out count);
                        for (int i = 0; i < count; i++)
                        {
                            IMMDevice dev;
                            if (col.Item(i, out dev) == 0 && dev != null)
                            {
                                string name = GetFriendlyName(dev).ToLower();
                                if (name.Contains("cable output") || (name.Contains("cable") && name.Contains("vb-audio")))
                                {
                                    string id;
                                    dev.GetId(out id);
                                    Console.WriteLine(id);
                                    return 0;
                                }
                            }
                        }
                    }
                }
                catch (Exception ex)
                {
                    Console.WriteLine("GetCableCapture error: " + ex.Message);
                    return 1;
                }
                return 1;
            }
            else if (args[0] == "--set-default-capture" && args.Length > 1)
            {
                try
                {
                    string targetId = args[1].Trim();
                    if (!string.IsNullOrEmpty(targetId))
                    {
                        if (SetAsDefault(targetId))
                        {
                            Console.WriteLine("Successfully set default capture endpoint to: " + targetId);
                            return 0;
                        }
                    }
                }
                catch (Exception ex)
                {
                    Console.WriteLine("SetDefaultCapture error: " + ex.Message);
                    return 1;
                }
                return 1;
            }
            else if (args[0] == "--restore-capture" && args.Length > 1)
            {
                try
                {
                    string id = null;
                    string arg = args[1].Trim();
                    if (File.Exists(arg))
                    {
                        id = File.ReadAllText(arg).Trim();
                    }
                    else
                    {
                        id = arg;
                    }

                    if (!string.IsNullOrEmpty(id))
                    {
                        IMMDevice testDev;
                        if (enumerator.GetDevice(id, out testDev) == 0 && testDev != null)
                        {
                            if (SetAsDefault(id))
                            {
                                Console.WriteLine("Successfully restored default capture endpoint to: " + id);
                                return 0;
                            }
                        }
                    }
                }
                catch { }

                return EnsurePhysicalCapture(enumerator);
            }
            else if (args[0] == "--ensure-physical-capture")
            {
                return EnsurePhysicalCapture(enumerator);
            }
            else if (args[0] == "--check-vbcable")
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

        static int EnsurePhysicalCapture(IMMDeviceEnumerator enumerator)
        {
            try
            {
                IMMDevice curDev = null;
                if (enumerator.GetDefaultAudioEndpoint(EDataFlow.eCapture, ERole.eConsole, out curDev) != 0 || curDev == null)
                {
                    enumerator.GetDefaultAudioEndpoint(EDataFlow.eCapture, ERole.eCommunications, out curDev);
                }
                if (curDev != null)
                {
                    string curName = GetFriendlyName(curDev).ToLower();
                    if (!curName.Contains("cable") && !curName.Contains("vb-audio"))
                    {
                        Console.WriteLine("Current default capture is already physical: " + curName);
                        return 0;
                    }
                }

                IMMDeviceCollection col;
                if (enumerator.EnumAudioEndpoints(EDataFlow.eCapture, 1 /* DEVICE_STATE_ACTIVE */, out col) == 0 && col != null)
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
                                    Console.WriteLine("Set default capture to physical device: " + name);
                                    return 0;
                                }
                            }
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine("EnsurePhysicalCapture error: " + ex.Message);
                return 1;
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
