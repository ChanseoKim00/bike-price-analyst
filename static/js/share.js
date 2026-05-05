// 결과 페이지 공유 카드 — 카톡/X/스레드/인스타/링크복사 동작.
// OG 이미지는 카톡·X·스레드가 URL에서 자동으로 가져온다. 인스타는 OG 미리보기를
// 거의 렌더하지 않으므로 이미지 다운로드를 제공해 스토리/피드 업로드를 유도.
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
    } catch (_) { /* analytics 실패는 무시 */ }
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
    // execCommand 폴백 — iOS Safari 구버전 등
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

  function openPopup(url) {
    var w = 600, h = 600;
    var left = (window.screen.width - w) / 2;
    var top  = (window.screen.height - h) / 2;
    var win = window.open(url, '_blank', 'width=' + w + ',height=' + h + ',left=' + left + ',top=' + top);
    if (!win) window.location.href = url;
  }

  // 카카오톡 — Kakao SDK 키가 있으면 SendDefault, 없으면 링크 복사로 폴백.
  // SDK 도입 전이라도 OG 이미지가 있는 URL을 채팅창에 붙여넣으면 미리보기가 뜬다.
  function shareKakao() {
    if (window.Kakao && window.Kakao.Share && window.Kakao.isInitialized && window.Kakao.isInitialized()) {
      window.Kakao.Share.sendDefault({
        objectType: 'feed',
        content: {
          title: shareTitle,
          description: 'Bike Price Analyst',
          imageUrl: ogUrl,
          link: { mobileWebUrl: shareUrl, webUrl: shareUrl },
        },
        buttons: [{
          title: '결과 보기',
          link: { mobileWebUrl: shareUrl, webUrl: shareUrl },
        }],
      });
      return;
    }
    copyToClipboard(shareUrl)
      .then(function () { showToast('링크를 복사했어요. 카카오톡 채팅창에 붙여넣어 주세요.'); })
      .catch(function () { showToast('링크 복사 실패. 주소창에서 직접 복사해 주세요.'); });
  }

  function shareX() {
    var url = 'https://twitter.com/intent/tweet'
      + '?text=' + encodeURIComponent(shareTitle)
      + '&url='  + encodeURIComponent(shareUrl);
    openPopup(url);
  }

  function shareThreads() {
    var text = shareTitle + '\n' + shareUrl;
    var url = 'https://www.threads.net/intent/post?text=' + encodeURIComponent(text);
    openPopup(url);
  }

  // 인스타그램 — 외부 URL 공유 API가 없어 OG 이미지 다운로드 + 링크 복사를 동시에 제공.
  // 사용자는 받은 이미지를 스토리/피드에 올리고, 복사된 링크를 프로필/스토리 링크에 첨부할 수 있다.
  function shareInstagram() {
    copyToClipboard(shareUrl).catch(function () {});
    var a = document.createElement('a');
    a.href = ogUrl;
    a.download = 'bpa-share.png';
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    showToast('이미지를 저장하고 링크를 복사했어요. 인스타에 올려보세요.');
  }

  function shareCopy() {
    copyToClipboard(shareUrl)
      .then(function () { showToast('링크를 복사했어요.'); })
      .catch(function () { showToast('링크 복사 실패. 주소창에서 직접 복사해 주세요.'); });
  }

  card.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-share]');
    if (!btn) return;
    var channel = btn.dataset.share;
    track(channel);
    switch (channel) {
      case 'kakao':     shareKakao(); break;
      case 'x':         shareX(); break;
      case 'threads':   shareThreads(); break;
      case 'instagram': shareInstagram(); break;
      case 'copy':      shareCopy(); break;
    }
  });
})();
