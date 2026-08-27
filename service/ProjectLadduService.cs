using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.ServiceProcess;
using System.Text.RegularExpressions;
using System.Threading;

public sealed class ProjectLadduService : ServiceBase
{
    private readonly object logLock = new object();
    private readonly string installDir;
    private Process backend;
    private Timer monitor;
    private DateTime backendStartedAt;
    private int rapidExitCount;
    private bool crashLoopOpen;
    private bool stopping;

    public ProjectLadduService()
    {
        ServiceName = "ProjectLaddu";
        CanStop = true;
        CanShutdown = true;
        AutoLog = true;
        installDir = Environment.GetEnvironmentVariable("PROJECT_LADDU_HOME");
        if (String.IsNullOrWhiteSpace(installDir))
            installDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "ProjectLaddu");
    }

    protected override void OnStart(string[] args)
    {
        stopping = false;
        crashLoopOpen = false;
        rapidExitCount = 0;
        Log("Service starting");
        StartBackend();
        monitor = new Timer(_ => MonitorBackend(), null, 5000, 5000);
    }

    private void MonitorBackend()
    {
        try
        {
            if (stopping || crashLoopOpen) return;
            if (backend == null) { StartBackend(); return; }
            if (!backend.HasExited) return;

            double uptime = (DateTime.UtcNow - backendStartedAt).TotalSeconds;
            int exitCode = backend.ExitCode;
            rapidExitCount = uptime < 30.0 ? rapidExitCount + 1 : 0;
            Log("Backend exited code=" + exitCode + " uptime_seconds=" + uptime.ToString("F1") + " rapid_exit_count=" + rapidExitCount);
            backend.Dispose();
            backend = null;

            if (rapidExitCount >= 6)
            {
                crashLoopOpen = true;
                Log("Backend crash-loop circuit opened after six rapid exits. Read backend.stderr.log and restart the service after correction.");
                return;
            }
            StartBackend();
        }
        catch (Exception ex) { Log("Monitor error: " + ex); }
    }

    private void StartBackend()
    {
        if (stopping || crashLoopOpen || (backend != null && !backend.HasExited)) return;

        string python = ResolvePython();
        string backendFile = Path.Combine(installDir, "backend", "main.py");
        if (!File.Exists(backendFile)) throw new FileNotFoundException("Installed backend entrypoint is missing", backendFile);

        var psi = new ProcessStartInfo();
        psi.FileName = python;
        psi.Arguments = "-X utf8 \"" + backendFile + "\"";
        psi.WorkingDirectory = Path.Combine(installDir, "backend");
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        psi.EnvironmentVariables["PROJECT_LADDU_HOME"] = installDir;
        psi.EnvironmentVariables["PROJECT_LADDU_PORT"] = ResolvePort();
        foreach (var pair in LoadProductionEnvironment()) psi.EnvironmentVariables[pair.Key] = pair.Value;

        backend = new Process();
        backend.StartInfo = psi;
        backend.EnableRaisingEvents = false;
        backend.OutputDataReceived += (_, e) => { if (e.Data != null) WriteBackendLog("stdout", e.Data); };
        backend.ErrorDataReceived += (_, e) => { if (e.Data != null) WriteBackendLog("stderr", e.Data); };
        if (!backend.Start()) throw new InvalidOperationException("Python backend process did not start.");
        backend.BeginOutputReadLine();
        backend.BeginErrorReadLine();
        backendStartedAt = DateTime.UtcNow;
        Log("Python backend started pid=" + backend.Id + " executable=" + python);
    }

    private Dictionary<string, string> LoadProductionEnvironment()
    {
        string path = Path.Combine(installDir, "secure", "data-plane.env.ps1");
        if (!File.Exists(path)) throw new FileNotFoundException("Production data-plane environment is missing. Run INSTALL_UPDATE.cmd from the complete release package.", path);
        var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var pattern = new Regex(@"^\s*\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'(.*)'\s*$");
        foreach (string line in File.ReadAllLines(path))
        {
            Match match = pattern.Match(line);
            if (!match.Success) continue;
            values[match.Groups[1].Value] = match.Groups[2].Value.Replace("''", "'");
        }
        string mode;
        if (!values.TryGetValue("PROJECT_LADDU_DATA_PLANE_MODE", out mode) || !String.Equals(mode, "production", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Production data-plane mode is not configured in " + path);
        foreach (string required in new [] { "PROJECT_LADDU_OPERATIONAL_DSN", "PROJECT_LADDU_GOVERNANCE_DSN", "PROJECT_LADDU_QUESTDB_HTTP_URL" })
            if (!values.ContainsKey(required) || String.IsNullOrWhiteSpace(values[required]))
                throw new InvalidDataException("Required data-plane variable is missing: " + required);
        return values;
    }

    protected override void OnStop()
    {
        stopping = true;
        Log("Service stopping");
        if (monitor != null) monitor.Dispose();
        monitor = null;
        StopBackend();
    }

    protected override void OnShutdown() { OnStop(); }

    private void StopBackend()
    {
        try
        {
            if (backend != null && !backend.HasExited)
            {
                var killer = new ProcessStartInfo("taskkill.exe", "/PID " + backend.Id + " /T /F");
                killer.UseShellExecute = false;
                killer.CreateNoWindow = true;
                Process.Start(killer).WaitForExit(5000);
            }
        }
        catch (Exception ex) { Log("Backend stop error: " + ex.Message); }
        finally
        {
            if (backend != null) backend.Dispose();
            backend = null;
        }
    }

    private string ResolvePython()
    {
        string pinned = Path.Combine(installDir, "runtime", "backend_python.txt");
        if (File.Exists(pinned))
        {
            string value = File.ReadAllText(pinned).Trim();
            if (File.Exists(value)) return value;
        }
        throw new FileNotFoundException("Pinned Project Laddu Python runtime is missing. Run INSTALL_UPDATE.cmd.", pinned);
    }

    private string ResolvePort()
    {
        string path = Path.Combine(installDir, "runtime", "port.txt");
        if (File.Exists(path))
        {
            string value = File.ReadAllText(path).Trim();
            int parsed;
            if (Int32.TryParse(value, out parsed) && parsed > 0 && parsed <= 65535) return parsed.ToString();
        }
        return "8086";
    }

    private string DailyLogPath(string name)
    {
        string folder = Path.Combine(installDir, "logs", DateTime.Now.ToString("yyyy-MM-dd"));
        Directory.CreateDirectory(folder);
        return Path.Combine(folder, name);
    }

    private void WriteBackendLog(string stream, string message)
    {
        string line = DateTime.Now.ToString("s") + " [" + stream + "] " + message + Environment.NewLine;
        lock (logLock)
        {
            File.AppendAllText(DailyLogPath("backend." + stream + ".log"), line);
            if (stream == "stderr") File.AppendAllText(Path.Combine(installDir, "logs", "backend-startup-error.log"), line);
        }
    }

    private void Log(string message)
    {
        string line = DateTime.Now.ToString("s") + " " + message + Environment.NewLine;
        lock (logLock)
        {
            File.AppendAllText(DailyLogPath("service.log"), line);
            File.AppendAllText(Path.Combine(installDir, "logs", "service.log"), line);
        }
    }

    public static void Main() { ServiceBase.Run(new ProjectLadduService()); }
}
