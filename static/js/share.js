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

  // 인스타그램 — 외부 URL 공유 API가 없고 OG 미리보기도 거의 안 뜨므로
  // 모바일은 시스템 공유 시트(Web Share API + 이미지 파일)로, 그 외는 새 탭에 OG 이미지를
  // 열어 사용자가 직접 저장하도록 한다. 링크는 항상 함께 복사.
  // 과거 <a download> 방식은 브라우저가 navigate로 처리할 때 "사이트를 사용할 수 없음"이
  // 뜨는 사례가 있어 폐기.
  function instagramFallback() {
    window.open(ogUrl, '_blank', 'noopener,noreferrer');
    copyToClipboard(shareUrl).catch(function () {});
    showToast('이미지가 새 탭에서 열렸어요. 저장해서 인스타에 올려보세요.');
  }

  function shareInstagram() {
    var isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
    if (isMobile && navigator.canShare && navigator.share && typeof fetch === 'function') {
      fetch(ogUrl)
        .then(function (r) { return r.ok ? r.blob() : Promise.reject(new Error('og fetch')); })
        .then(function (blob) {
          var file = new File([blob], 'bpa-share.png', { type: 'image/png' });
          if (!navigator.canShare({ files: [file] })) return Promise.reject(new Error('canShare'));
          return navigator.share({
            files: [file],
            title: shareTitle,
            text: shareTitle + '\n' + shareUrl,
          });
        })
        .catch(function () { instagramFallback(); });
      return;
    }
    instagramFallback();
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
