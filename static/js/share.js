// Result page share card — X / Threads / Reddit / Instagram (image download) / copy link.
//
// Channel behavior:
//   X · Threads · Reddit : official intent URLs opened in a popup (desktop + mobile).
//   Instagram            : Instagram has no public web share intent and rarely renders
//                          OG previews, so we download the OG image and copy the link
//                          so the user can paste it into a story / post / profile bio.
//   Copy link            : always copies to the clipboard.
//
// X / Threads / Reddit pull the OG image automatically from the URL preview. Instagram
// gets a separate image-download path because IG doesn't unfurl link previews.
(function () {
  'use strict';

  var card = document.querySelector('.share-card');
  if (!card) return;

  var shareUrl   = card.dataset.shareUrl;
  var shareTitle = card.dataset.shareTitle;
  var ogUrl      = card.dataset.ogUrl;
  var toastEl    = document.getElementById('shareToast');

  function track(channel) {
    try {
      if (window.bpa && typeof window.bpa.capture === 'function') {
        window.bpa.capture('share_card_click', { channel: channel });
      } else if (window.posthog && typeof window.posthog.capture === 'function') {
        window.posthog.capture('share_card_click', { channel: channel });
      }
    } catch (_) { /* swallow analytics failures */ }
  }

  var toastTimer = null;
  function showToast(msg) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.classList.add('is-visible');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastEl.classList.remove('is-visible');
    }, 2400);
  }

  function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    // execCommand fallback — older iOS Safari, etc.
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        ok ? resolve() : reject(new Error('copy failed'));
      } catch (e) { reject(e); }
    });
  }

  function copyLink(toastMsg) {
    copyToClipboard(shareUrl)
      .then(function () { showToast(toastMsg || 'Link copied.'); })
      .catch(function () { showToast('Could not copy. Please copy the URL from the address bar.'); });
  }

  function openPopup(url) {
    var w = 600, h = 600;
    var left = (window.screen.width - w) / 2;
    var top  = (window.screen.height - h) / 2;
    var win = window.open(url, '_blank', 'width=' + w + ',height=' + h + ',left=' + left + ',top=' + top);
    if (!win) window.location.href = url;
  }

  // Trigger a download of the OG image so the user can post it on Instagram.
  // Returns a promise that resolves once the download has been kicked off.
  function downloadOgImage() {
    return fetch(ogUrl)
      .then(function (r) { return r.ok ? r.blob() : Promise.reject(new Error('og fetch')); })
      .then(function (blob) {
        var blobUrl = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = blobUrl;
        a.download = 'bpa-share.png';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        // Free the blob URL after the click has had time to start the download.
        setTimeout(function () { URL.revokeObjectURL(blobUrl); }, 1000);
      });
  }

  function shareInstagram() {
    // Download the share image and copy the link so the user can paste it
    // into their Instagram story / post / profile bio.
    downloadOgImage()
      .then(function () {
        return copyToClipboard(shareUrl).catch(function () { /* ignore copy errors here */ });
      })
      .then(function () {
        showToast('Image downloaded and link copied. Paste it into your Instagram post.');
      })
      .catch(function () {
        // If image download fails, fall back to just copying the link.
        copyLink('Link copied. Paste it into your Instagram post.');
      });
  }

  function shareX() {
    var url = 'https://twitter.com/intent/tweet'
      + '?text=' + encodeURIComponent(shareTitle)
      + '&url='  + encodeURIComponent(shareUrl);
    openPopup(url);
  }

  function shareThreads() {
    var text = shareTitle + ' ' + shareUrl;
    var url = 'https://www.threads.net/intent/post?text=' + encodeURIComponent(text);
    openPopup(url);
  }

  function shareReddit() {
    var url = 'https://www.reddit.com/submit'
      + '?url='   + encodeURIComponent(shareUrl)
      + '&title=' + encodeURIComponent(shareTitle);
    openPopup(url);
  }

  function shareCopy() {
    copyLink('Link copied.');
  }

  card.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-share]');
    if (!btn) return;
    var channel = btn.dataset.share;
    track(channel);
    switch (channel) {
      case 'x':         shareX(); break;
      case 'threads':   shareThreads(); break;
      case 'reddit':    shareReddit(); break;
      case 'instagram': shareInstagram(); break;
      case 'copy':      shareCopy(); break;
    }
  });
})();
