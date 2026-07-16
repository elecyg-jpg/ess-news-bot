import os
import requests
import feedparser
import urllib.parse
import google.generativeai as genai

# 환경변수에서 API 키 불러오기 (GitHub Secrets에서 설정할 예정)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Gemini AI 모델 설정
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.5-flash') # 빠르고 저렴한 최신 모델

# 1. 구글 뉴스 RSS URL 생성 (키워드 적용)
keywords = '("배전망 ESS" OR "배전망 에너지저장장치" OR "AI 활용 ESS" OR "VPP" OR "가상발전소" OR "재생에너지 계통연계" OR "접속대기" OR "계통혼잡" OR "출력제어" OR "동적운영한계" OR "Dynamic Operating Envelope" OR "BESS")'
encoded_query = urllib.parse.quote(keywords)
rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"

def get_news_and_summarize():
    try:
        # 2. 뉴스 수집
        feed = feedparser.parse(rss_url)
        if not feed.entries:
            send_telegram_message("👀 현재 시간 기준으로 새로운 뉴스 기사가 없습니다.")
            return

        # 최신 기사 5개만 추출
        news_texts = []
        links_text = ""
        for i, entry in enumerate(feed.entries[:5]):
            news_texts.append(f"[기사 {i+1}] 제목: {entry.title}\n요약문: {entry.description}")
            links_text += f"{i+1}. {entry.title}\n{entry.link}\n\n"

        combined_news = "\n\n".join(news_texts)

        # 3. AI에게 분석 및 요약 지시 (프롬프트)
        prompt = f"""
        당신은 전력망 및 에너지저장장치(ESS) 산업의 전문 분석가입니다. 
        아래는 오늘 수집된 관련 최신 뉴스 기사들입니다.
        이 기사들을 종합하여 산업의 주요 동향을 파악하고, 핵심 내용만 3~5줄의 글머리기호(Bullet point) 형식으로 명확하게 요약해주세요.
        
        {combined_news}
        """
        
        response = model.generate_content(prompt)
        summary = response.text

        # 4. 텔레그램 메시지 구성
        final_message = f"⚡ [배전망/ESS/VPP 주요 뉴스 브리핑]\n\n{summary}\n\n🔗 [기사 원문 링크]\n{links_text}"
        
        # 5. 전송
        send_telegram_message(final_message)

    except Exception as e:
        send_telegram_message(f"🚨 뉴스 요약 중 오류가 발생했습니다: {e}")

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    requests.post(url, data=payload)

if __name__ == "__main__":
    get_news_and_summarize()
