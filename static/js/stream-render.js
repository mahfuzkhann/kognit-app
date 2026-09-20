/* ============================================================
   KOGNIT STREAM RENDERING — PHASE 8E
   ============================================================
   Safe incremental rendering for streamed answers.

   THE PROBLEM THIS SOLVES

   A stream delivers text at arbitrary boundaries. Gemini can and
   will split a chunk in the middle of "$$F = ma$$", in the middle
   of a ```code fence, or halfway through a **bold** run. Feeding
   that partial text straight to marked.parse() every frame gives
   you flickering half-parsed markup, and handing an unterminated
   "$$" to MathJax can leave permanently corrupted output that
   never recovers even after the rest arrives.

   Kognit's Bengali + LaTeX pipeline makes this sharper than
   usual: app.js already has to protect math segments from
   marked.js before parsing (see protectMathSegments there), and
   that protection assumes it is looking at COMPLETE segments.

   THE APPROACH

   Never parse a partial construct. findSafeBoundary(text) returns
   the largest index such that text.slice(0, index) contains no
   unterminated code fence, display-math block, inline-math span
   or inline-emphasis run. Everything up to that index is parsed
   normally; the remainder is shown as escaped plain text until
   its closing delimiter arrives.

   The student therefore sees text appear immediately, and each
   construct "snaps" into rendered form the moment it completes.
   Nothing is delayed artificially - there is no timer anywhere in
   this file.
   ============================================================ */

(function () {
    "use strict";

    /* --------------------------------------------------------
       Loading copy. Mirrors the backend's `context` values from
       the "start" event. The backend decides these from real
       state (is research on, is a PDF attached) - the frontend
       never guesses, which is why "Reading your document" can no
       longer appear when no document exists.
       -------------------------------------------------------- */
    var LOADING_COPY = {
        thinking: "Thinking\u2026",
        research: "Searching the web\u2026",
        document: "Reading your document\u2026",
        document_web: "Checking your document and the web\u2026"
    };

    function loadingCopyFor(context) {
        return LOADING_COPY[context] || LOADING_COPY.thinking;
    }

    /* --------------------------------------------------------
       Safe-boundary scanner
       --------------------------------------------------------
       Single left-to-right pass. Tracks the last position at
       which no construct was open; that position is the safe
       boundary. Deliberately conservative: when in doubt it
       returns a smaller index, which only means a little more
       text renders as plain for a few milliseconds longer.
       -------------------------------------------------------- */
    function findSafeBoundary(text) {
        if (!text) return 0;

        var i = 0;
        var len = text.length;
        var safe = 0;

        while (i < len) {
            var ch = text[i];

            // ---- fenced code block: ``` ... ```
            if (text.startsWith("```", i)) {
                var closeFence = text.indexOf("```", i + 3);
                if (closeFence === -1) {
                    // Unterminated fence - everything from here is unsafe.
                    return _clampTrailingDelimiters(text, safe);
                }
                i = closeFence + 3;
                // A fence is only complete once its closing line ends;
                // treat the position after the closing ``` as safe.
                safe = i;
                continue;
            }

            // ---- display math: $$ ... $$
            if (text.startsWith("$$", i)) {
                var closeDisplay = text.indexOf("$$", i + 2);
                if (closeDisplay === -1) return _clampTrailingDelimiters(text, safe);
                i = closeDisplay + 2;
                safe = i;
                continue;
            }

            // ---- LaTeX display: \[ ... \]
            if (text.startsWith("\\[", i)) {
                var closeBracket = text.indexOf("\\]", i + 2);
                if (closeBracket === -1) return _clampTrailingDelimiters(text, safe);
                i = closeBracket + 2;
                safe = i;
                continue;
            }

            // ---- LaTeX inline: \( ... \)
            if (text.startsWith("\\(", i)) {
                var closeParen = text.indexOf("\\)", i + 2);
                if (closeParen === -1) return _clampTrailingDelimiters(text, safe);
                i = closeParen + 2;
                safe = i;
                continue;
            }

            // ---- inline math: $ ... $  (single, not part of $$)
            if (ch === "$") {
                var closeInline = text.indexOf("$", i + 1);
                if (closeInline === -1) return _clampTrailingDelimiters(text, safe);
                i = closeInline + 1;
                safe = i;
                continue;
            }

            // ---- inline code: ` ... `
            if (ch === "`") {
                var closeCode = text.indexOf("`", i + 1);
                if (closeCode === -1) return _clampTrailingDelimiters(text, safe);
                i = closeCode + 1;
                safe = i;
                continue;
            }

            // ---- emphasis runs: ** ... ** and * ... *
            if (text.startsWith("**", i)) {
                var closeBold = text.indexOf("**", i + 2);
                if (closeBold === -1) return _clampTrailingDelimiters(text, safe);
                i = closeBold + 2;
                safe = i;
                continue;
            }

            i += 1;
            // Plain characters are always safe to render. This is what makes
            // text appear the instant it arrives rather than waiting for the
            // end of a line: "open $$F = " renders "open " immediately and
            // holds back only the unterminated math.
            safe = i;
        }

        // Nothing left open. Clamp below.
        return _clampTrailingDelimiters(text, len);
    }

    /* --------------------------------------------------------
       _clampTrailingDelimiters
       --------------------------------------------------------
       A delimiter run sitting at the very END of the buffer is
       ambiguous: "``" may be a complete empty inline-code span,
       or it may be the first two characters of a "```" fence that
       has not finished arriving. Likewise "*" vs "**".

       Without this, the boundary could move BACKWARDS between
       chunks - "``" parses as complete, then "```" is recognised
       as an unterminated fence and the boundary retreats. A
       retreating boundary means already-rendered text un-renders,
       which the student sees as a flicker.

       So: never let the safe boundary extend into a trailing run
       of delimiter characters. Costs at most a few characters of
       delay, and guarantees the boundary only ever grows.
       -------------------------------------------------------- */
    var AMBIGUOUS_TRAILING = "`*$\\_~";

    function _clampTrailingDelimiters(text, boundary) {
        var start = text.length;
        while (start > 0 && AMBIGUOUS_TRAILING.indexOf(text[start - 1]) !== -1) {
            start -= 1;
        }
        return Math.min(boundary, start);
    }

    /* --------------------------------------------------------
       Split streamed text into (renderable, pending) halves.
       -------------------------------------------------------- */
    function splitForRender(text) {
        var boundary = findSafeBoundary(text);
        return {
            renderable: text.slice(0, boundary),
            pending: text.slice(boundary)
        };
    }

    /* --------------------------------------------------------
       hasCompleteMath - should MathJax run on this fragment?
       Avoids paying for a typeset pass on text containing no math
       at all, and guarantees we never typeset an unterminated
       display-math block.
       -------------------------------------------------------- */
    function hasCompleteMath(text) {
        if (!text) return false;
        if (/\$\$[\s\S]+?\$\$/.test(text)) return true;
        if (/\\\[[\s\S]+?\\\]/.test(text)) return true;
        if (/\\\([\s\S]+?\\\)/.test(text)) return true;
        if (/\$[^$\n]+\$/.test(text)) return true;
        return false;
    }

    /* --------------------------------------------------------
       NDJSON line reader over a fetch() body stream.
       Yields parsed objects. Tolerates chunk boundaries that
       split a line, and skips malformed lines rather than
       aborting the whole answer.
       -------------------------------------------------------- */
    function createNdjsonParser(onEvent, onMalformed) {
        var buffer = "";
        return {
            push: function (textChunk) {
                buffer += textChunk;
                var lines = buffer.split("\n");
                // Last element is either "" (clean boundary) or a partial
                // line that must wait for the next chunk.
                buffer = lines.pop();
                for (var i = 0; i < lines.length; i++) {
                    var line = lines[i].trim();
                    if (!line) continue;
                    try {
                        onEvent(JSON.parse(line));
                    } catch (e) {
                        if (onMalformed) onMalformed(line);
                    }
                }
            },
            flush: function () {
                var line = buffer.trim();
                buffer = "";
                if (!line) return;
                try {
                    onEvent(JSON.parse(line));
                } catch (e) {
                    if (onMalformed) onMalformed(line);
                }
            }
        };
    }

    window.KognitStream = {
        LOADING_COPY: LOADING_COPY,
        loadingCopyFor: loadingCopyFor,
        findSafeBoundary: findSafeBoundary,
        splitForRender: splitForRender,
        hasCompleteMath: hasCompleteMath,
        createNdjsonParser: createNdjsonParser
    };
})();