package com.trading.bookmap;

import java.io.BufferedWriter;
import java.io.FileWriter;
import java.io.IOException;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;

/**
 * Append-only diagnostic log file written to a stable, user-accessible
 * path ({@code $HOME/bookmap-addon-debug.log}).
 *
 * <p>Bookmap captures SLF4J output to its own log directory whose
 * location varies by platform and is not always obvious to users.
 * This class provides a parallel, predictable destination for the
 * add-on's most important diagnostics (connection lifecycle,
 * supersession, exceptions) so they can be quickly inspected with
 * {@code tail -f}.
 *
 * <p>All methods are safe to call from any thread; writes are
 * serialised on a class-level lock. Failures to write are swallowed
 * so a broken filesystem can never crash the add-on.
 */
public final class DiagnosticLog {

    private static final Object WRITE_LOCK = new Object();
    private static final DateTimeFormatter TS =
        DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss.SSS");
    private static final Path PATH = resolvePath();
    private static volatile boolean openedThisRun = false;

    private DiagnosticLog() {
    }

    /** Absolute path to the diagnostic log file. */
    public static Path path() {
        return PATH;
    }

    /** Write a single timestamped line. */
    public static void log(String line) {
        write(line, null);
    }

    /** Write a line with an attached throwable's stack trace. */
    public static void log(String line, Throwable t) {
        write(line, t);
    }

    private static void write(String line, Throwable t) {
        synchronized (WRITE_LOCK) {
            try (BufferedWriter w = new BufferedWriter(
                    new FileWriter(PATH.toFile(), true))) {
                if (!openedThisRun) {
                    openedThisRun = true;
                    w.write(banner());
                }
                w.write(LocalDateTime.now().format(TS));
                w.write("  ");
                w.write(line);
                w.write(System.lineSeparator());
                if (t != null) {
                    StringWriter sw = new StringWriter();
                    t.printStackTrace(new PrintWriter(sw));
                    w.write(sw.toString());
                }
                w.flush();
            } catch (IOException ignored) {
                // Best-effort logging — never propagate IO failure.
            }
        }
    }

    private static String banner() {
        return System.lineSeparator()
            + "=========================================================="
            + System.lineSeparator()
            + " Bookmap Strategy Add-on diagnostic log opened"
            + System.lineSeparator()
            + "=========================================================="
            + System.lineSeparator();
    }

    private static Path resolvePath() {
        String home = System.getProperty("user.home");
        if (home == null || home.isEmpty()) {
            return Paths.get("bookmap-addon-debug.log");
        }
        Path p = Paths.get(home, "bookmap-addon-debug.log");
        try {
            Files.createDirectories(p.getParent());
        } catch (Exception ignored) {
            // best-effort
        }
        return p;
    }
}
