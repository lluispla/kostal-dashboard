/* =========================================================================
   Kiosk Mode — auto-rotate sections, touch to pause/navigate
   ========================================================================= */

(function () {
    'use strict';

    if (!document.body.classList.contains('kiosk')) return;

    var ROTATE_INTERVAL = 10000; // ms per section
    var sections = document.querySelectorAll('.section');
    var current = 0;
    var timer = null;
    var paused = false;

    if (sections.length === 0) return;

    // Create navigation dots
    var dotsWrap = document.createElement('div');
    dotsWrap.className = 'kiosk-dots';
    for (var i = 0; i < sections.length; i++) {
        var dot = document.createElement('button');
        dot.className = 'kiosk-dot';
        dot.dataset.idx = i;
        dot.addEventListener('click', function () {
            goTo(parseInt(this.dataset.idx));
        });
        dotsWrap.appendChild(dot);
    }
    document.body.appendChild(dotsWrap);

    // Create progress bar
    var progress = document.createElement('div');
    progress.className = 'kiosk-progress';
    document.body.appendChild(progress);

    function showSection(idx) {
        for (var i = 0; i < sections.length; i++) {
            sections[i].classList.toggle('kiosk-active', i === idx);
        }
        var dots = dotsWrap.querySelectorAll('.kiosk-dot');
        for (var j = 0; j < dots.length; j++) {
            dots[j].classList.toggle('active', j === idx);
        }
        current = idx;
        // Reset progress bar
        progress.style.transition = 'none';
        progress.style.width = '0%';
        // Force reflow then animate
        progress.offsetWidth;
        progress.style.transition = 'width ' + ROTATE_INTERVAL + 'ms linear';
        progress.style.width = '100%';
    }

    function goTo(idx) {
        showSection(idx);
        resetTimer();
    }

    function next() {
        if (paused) return;
        showSection((current + 1) % sections.length);
    }

    function resetTimer() {
        if (timer) clearInterval(timer);
        timer = setInterval(next, ROTATE_INTERVAL);
    }

    // Touch/click: tap left half = prev, right half = next, hold = pause
    var holdTimer = null;
    document.addEventListener('touchstart', function (e) {
        holdTimer = setTimeout(function () {
            paused = !paused;
            progress.style.transition = 'none';
            progress.style.width = paused ? '0%' : '100%';
            holdTimer = null;
        }, 600);
    });
    document.addEventListener('touchend', function (e) {
        if (holdTimer) {
            clearTimeout(holdTimer);
            // Short tap: navigate
            var x = e.changedTouches[0].clientX;
            if (x < window.innerWidth / 2) {
                goTo((current - 1 + sections.length) % sections.length);
            } else {
                goTo((current + 1) % sections.length);
            }
        }
    });

    // Start
    showSection(0);
    resetTimer();
})();
