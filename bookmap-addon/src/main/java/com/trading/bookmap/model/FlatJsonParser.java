package com.trading.bookmap.model;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Minimal, dependency-free JSON parser for the flat string→primitive
 * map shape used by the Bookmap publisher's wire schema.
 *
 * <h3>Why hand-rolled?</h3>
 * The fat-jar lives inside Bookmap's process where its classloader
 * pre-loads older copies of common libraries — most recently Jackson
 * (<code>JsonParser.getReadCapabilities()</code> missing on Bookmap's
 * 2.12-ish Jackson, breaking our 2.17.x <code>DeserializationContext</code>).
 * Bringing in a bigger library only moves the conflict; relocating
 * Jackson via a shading plugin would also work but adds significant
 * build-system surface area for a schema this trivial.
 *
 * <h3>Supported grammar</h3>
 * <ul>
 *   <li>A single top-level JSON object: <code>{ "k": v, ... }</code></li>
 *   <li>Keys are JSON strings (always quoted, double-quotes only).</li>
 *   <li>Values are one of:
 *     <ul>
 *       <li>JSON string (double-quoted) with standard backslash escapes
 *           (quote, backslash, slash, b, f, n, r, t, and 4-hex unicode)</li>
 *       <li>true / false: {@link Boolean}</li>
 *       <li>null: mapped to absent key</li>
 *       <li>integer literal: {@link Long}</li>
 *       <li>floating-point literal (contains '.', 'e', or 'E'): {@link Double}</li>
 *     </ul>
 *   </li>
 *   <li>Nested objects / arrays are <b>not</b> supported (our schema is flat).</li>
 * </ul>
 *
 * <p>Whitespace around tokens is tolerated. The parser is strict on
 * structural punctuation but lenient on trailing whitespace.
 */
public final class FlatJsonParser {

    private FlatJsonParser() {
    }

    /**
     * Parse a JSON object literal into a {@link Map}. Returns an empty
     * map when input is {@code null} or empty; throws
     * {@link IllegalArgumentException} on any structural error.
     */
    public static Map<String, Object> parseObject(String json) {
        if (json == null) {
            return new LinkedHashMap<>();
        }
        Parser p = new Parser(json);
        p.skipWs();
        if (p.eof()) {
            return new LinkedHashMap<>();
        }
        Map<String, Object> out = p.readObject();
        p.skipWs();
        if (!p.eof()) {
            throw new IllegalArgumentException(
                "Trailing characters at position " + p.pos);
        }
        return out;
    }

    /** Lookup helpers with safe coercion + defaults. */
    public static String getString(Map<String, Object> map, String key, String def) {
        Object v = map.get(key);
        if (v == null) {
            return def;
        }
        return v.toString();
    }

    public static long getLong(Map<String, Object> map, String key, long def) {
        Object v = map.get(key);
        if (v instanceof Number) {
            return ((Number) v).longValue();
        }
        if (v instanceof String) {
            try {
                return Long.parseLong((String) v);
            } catch (NumberFormatException ex) {
                return def;
            }
        }
        return def;
    }

    public static double getDouble(Map<String, Object> map, String key, double def) {
        Object v = map.get(key);
        if (v instanceof Number) {
            return ((Number) v).doubleValue();
        }
        if (v instanceof String) {
            try {
                return Double.parseDouble((String) v);
            } catch (NumberFormatException ex) {
                return def;
            }
        }
        return def;
    }

    // ------------------------------------------------------------------ impl

    private static final class Parser {
        final String src;
        int pos;

        Parser(String src) {
            this.src = src;
            this.pos = 0;
        }

        boolean eof() {
            return pos >= src.length();
        }

        void skipWs() {
            while (pos < src.length()) {
                char c = src.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    pos++;
                } else {
                    break;
                }
            }
        }

        void expect(char c) {
            if (eof() || src.charAt(pos) != c) {
                throw new IllegalArgumentException(
                    "Expected '" + c + "' at position " + pos
                        + " in '" + abbreviate(src) + "'");
            }
            pos++;
        }

        Map<String, Object> readObject() {
            expect('{');
            Map<String, Object> out = new LinkedHashMap<>();
            skipWs();
            if (!eof() && src.charAt(pos) == '}') {
                pos++;
                return out;
            }
            while (true) {
                skipWs();
                String key = readString();
                skipWs();
                expect(':');
                skipWs();
                Object value = readValue();
                if (value != Null.INSTANCE) {
                    out.put(key, value);
                }
                skipWs();
                if (!eof() && src.charAt(pos) == ',') {
                    pos++;
                    continue;
                }
                expect('}');
                return out;
            }
        }

        Object readValue() {
            if (eof()) {
                throw new IllegalArgumentException(
                    "Unexpected end of input at " + pos);
            }
            char c = src.charAt(pos);
            if (c == '"') {
                return readString();
            }
            if (c == 't' || c == 'f') {
                return readBoolean();
            }
            if (c == 'n') {
                expectLiteral("null");
                return Null.INSTANCE;
            }
            if (c == '-' || (c >= '0' && c <= '9')) {
                return readNumber();
            }
            throw new IllegalArgumentException(
                "Unexpected character '" + c + "' at position " + pos);
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (!eof()) {
                char c = src.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    if (eof()) {
                        throw new IllegalArgumentException(
                            "Dangling backslash at end of input");
                    }
                    char esc = src.charAt(pos++);
                    switch (esc) {
                        case '"': sb.append('"'); break;
                        case '\\': sb.append('\\'); break;
                        case '/': sb.append('/'); break;
                        case 'b': sb.append('\b'); break;
                        case 'f': sb.append('\f'); break;
                        case 'n': sb.append('\n'); break;
                        case 'r': sb.append('\r'); break;
                        case 't': sb.append('\t'); break;
                        case 'u':
                            if (pos + 4 > src.length()) {
                                throw new IllegalArgumentException(
                                    "Truncated unicode escape at " + pos);
                            }
                            sb.append((char) Integer.parseInt(
                                src.substring(pos, pos + 4), 16));
                            pos += 4;
                            break;
                        default:
                            throw new IllegalArgumentException(
                                "Bad escape '\\" + esc + "' at " + (pos - 1));
                    }
                } else {
                    sb.append(c);
                }
            }
            throw new IllegalArgumentException("Unterminated string literal");
        }

        Boolean readBoolean() {
            if (src.charAt(pos) == 't') {
                expectLiteral("true");
                return Boolean.TRUE;
            }
            expectLiteral("false");
            return Boolean.FALSE;
        }

        void expectLiteral(String literal) {
            int len = literal.length();
            if (pos + len > src.length()
                    || !src.regionMatches(pos, literal, 0, len)) {
                throw new IllegalArgumentException(
                    "Expected literal '" + literal + "' at position " + pos);
            }
            pos += len;
        }

        Number readNumber() {
            int start = pos;
            if (src.charAt(pos) == '-') {
                pos++;
            }
            boolean isFloat = false;
            while (!eof()) {
                char c = src.charAt(pos);
                if (c >= '0' && c <= '9') {
                    pos++;
                } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                    isFloat = true;
                    pos++;
                } else {
                    break;
                }
            }
            String token = src.substring(start, pos);
            try {
                if (isFloat) {
                    return Double.parseDouble(token);
                }
                return Long.parseLong(token);
            } catch (NumberFormatException ex) {
                throw new IllegalArgumentException(
                    "Bad number literal '" + token + "' at " + start, ex);
            }
        }

        String abbreviate(String s) {
            if (s.length() <= 80) {
                return s;
            }
            return s.substring(0, 80) + "...";
        }
    }

    /** Sentinel for JSON null so callers can distinguish from absent keys. */
    private static final class Null {
        static final Null INSTANCE = new Null();
    }
}
