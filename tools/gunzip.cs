using System;
using System.IO;
using System.IO.Compression;

// Minimal gunzip replacement for gnssrefl's subprocess.call(['gunzip', path])
// calls (see gps.py and friends): Windows CreateProcess only auto-appends
// ".exe" for a bare command name, never runs .bat/.cmd this way, and the
// Git-for-Windows gzip.exe binary does not implement the classic Unix
// "behave as gunzip when invoked under that name" dispatch - so gnssrefl's
// calls silently no-op ("already has .gz suffix -- unchanged") instead of
// decompressing. This is a real, dedicated executable instead.
class Gunzip
{
    static int Main(string[] args)
    {
        foreach (var arg in args)
        {
            if (arg.StartsWith("-")) continue; // ignore flags like -f
            string inPath = arg;
            if (!File.Exists(inPath))
            {
                Console.Error.WriteLine("gunzip: " + inPath + ": No such file or directory");
                return 1;
            }
            string outPath = inPath.EndsWith(".gz")
                ? inPath.Substring(0, inPath.Length - 3)
                : inPath + ".out";
            using (var inStream = File.OpenRead(inPath))
            using (var gzStream = new GZipStream(inStream, CompressionMode.Decompress))
            using (var outStream = File.Create(outPath))
            {
                gzStream.CopyTo(outStream);
            }
            File.Delete(inPath);
        }
        return 0;
    }
}
