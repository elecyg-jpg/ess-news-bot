import os
import requests
import feedparser
import urllib.parse
from bs4 import BeautifulSoup
import google.generativeai as genai

# 환경변수 설정
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
# 최신 모델 사용으로 긴 문맥 처리 완벽 지원
model = genai.GenerativeModel('gemini-3.5-flash')

keywords = '("배전망 ESS" OR "배전망 에너지저장장치" OR "AI 활용 ESS" OR "VPP" OR "가상발전소" OR "재생에너지 계통연계" OR "접속대기" OR "계통혼잡" OR "출력제어" OR "동적운영한계" OR "Dynamic Operating Envelope" OR "BESS")'
encoded_query = urllib.parse.quote(keywords)
rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"

def fetch_article_data(google_url):
    try:
        # 1. 브라우저인 것처럼 위장하여 접근 차단 방지
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(google_url, headers=headers, timeout=15)
        actual_url = response.url
        
        # 2. 기사 본문 추출 (불순물 제거)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 광고, 메뉴, 스크립트 등 불필요한 태그 제거
        for script in soup(["script", "style", "header", "footer", "nav"]):
            script.extract()
            
        # 3. 글자 수 제한 해제 (무제한 크롤링)
        # [:6000] 제한을 없애고 전체 텍스트를 모두 수집합니다.
        text = soup.get_text(separator=' ', strip=True)
        
        return actual_url, text
    except:
        return google_url, ""

def get_news_and_summarize():
    try:
        feed = feedparser.parse(rss_url)
        if not feed.entries:
            send_telegram_message("👀 현재 시간 기준으로 새로운 뉴스 기사가 없습니다.")
            return

        news_texts = []
        for i, entry in enumerate(feed.entries[:5]):
            title = entry.title
            google_link = entry.link
            source = entry.source.title if 'source' in entry else "신문사 미상"
            
            actual_link, article_text = fetch_article_data(google_link)
            
            news_texts.append(f"[기사 {i+1}]\n제목: {title}\n신문사: {source}\n원본링크: {actual_link}\n기사본문(전체): {article_text}")

        combined_news = "\n\n".join(news_texts)

        # 4. 데이터 추출을 강력하게 강제하는 프롬프트
        prompt = f"""
        당신은 전력망 및 에너지저장장치(ESS) 산업의 전문 분석가입니다. 
        아래 제공된 [기사 원본 데이터]를 분석하여 부서장에게 보고할 최고 수준의 브리핑을 작성해주세요.
        
        [정확한 출력 양식]
        1. [{{기사제목}}]
        ㅇ {{기자명}} 기자 | {{기자이메일}} | 승인 {{승인일(YYYY.MM.DD HH:MM)}} | {{신문사명}}
        ㅇ (기사의 핵심 주제 1줄 요약)
          - (세부 내용 1)
          - (세부 내용 2)
          - 기사 원문 링크: {{원본링크}}
        
        [데이터 추출 절대 규칙 - 매우 중요!]
        - '기사본문(전체)' 텍스트 안에 있는 기자의 실명, 이메일 주소(@ 포함), 날짜(승인, 입력, 기사출고 등의 단어 뒤)를 '반드시' 찾아내서 양식에 채우세요.
        - 텍스트 하단에 "안성렬 기자 youan5019@cstimes.com 기사출고 2026년 07월 16일"과 같은 정보가 숨어있으니 절대 놓치지 마세요.
        - 정밀하게 탐색했음에도 정말 정보가 없다면 '기자 미상', '이메일 미상'으로 기재하세요.
        - 반드시 제공된 '원본링크'를 그대로 사용하세요.

        [기사 원본 데이터]
        {combined_news}
        """
        
        response = model.generate_content(prompt)
        summary = response.text

        final_message = f"⚡ [배전망/ESS 주요 뉴스 브리핑]\n\n{summary}"
        send_telegram_message(final_message)

    except Exception as e:
        send_telegram_message(f"🚨 뉴스 요약 중 오류가 발생했습니다: {e}")

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    requests.post(url, data=payload)

if __name__ == "__main__":
    get_news_and_summarize()
