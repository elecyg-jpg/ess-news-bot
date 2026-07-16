import os
import requests
import feedparser
import urllib.parse
import email.utils
import google.generativeai as genai

# 환경변수에서 API 키 불러오기
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Gemini AI 모델 설정 (버전 유지)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.5-flash')

# 1. 구글 뉴스 RSS URL 생성 (키워드 유지)
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

        # 최신 기사 5개 추출 및 데이터 정제
        news_texts = []
        for i, entry in enumerate(feed.entries[:5]):
            title = entry.title
            link = entry.link
            description = entry.description
            
            # 날짜 변환 및 출처(신문사) 추출
            try:
                parsed_date = email.utils.parsedate_to_datetime(entry.pubDate)
                formatted_date = parsed_date.strftime("%Y-%m-%d")
            except:
                formatted_date = "날짜 미상"
            
            source = entry.source.title if 'source' in entry else "신문사 미상"
            
            news_texts.append(f"[기사 데이터 {i+1}]\n제목: {title}\n날짜: {formatted_date}\n신문사: {source}\n본문요약: {description}\n링크: {link}")

        combined_news = "\n\n".join(news_texts)

        # 3. AI에게 엄격한 양식 지시 (프롬프트 변경)
        prompt = f"""
        당신은 전력망 및 에너지저장장치(ESS) 산업의 전문 분석가입니다. 
        아래 제공된 뉴스 기사 데이터를 바탕으로, [정확한 출력 양식]에 맞게 브리핑을 작성해주세요.
        
        [정확한 출력 양식]
        1. [{{기사제목}}] ({{발행날짜}} / {{신문사명}} / {{기자명}} / {{기자이메일}})
        ㅇ (기사의 핵심 주제 1줄 요약)
          - (세부 내용 1)
          - (세부 내용 2)
          - {{기사원문링크}}
        
        2. [{{기사제목}}] ... (이하 반복)

        [주의사항]
        - 절대로 줄글 형태로 길게 나열하지 마세요. 반드시 위 양식의 줄바꿈과 들여쓰기를 지켜서 가독성을 극대화하세요.
        - RSS 데이터 특성상 본문에 기자명이나 이메일이 파악되지 않으면 생략하거나 '기자 미상'으로 기재하세요.
        - 각 기사의 마지막 글머리기호에는 반드시 해당 기사의 [링크]를 원문 그대로 넣어주세요.

        [수집된 기사 데이터]
        {combined_news}
        """
        
        response = model.generate_content(prompt)
        summary = response.text

        # 4. 텔레그램 메시지 구성
        final_message = f"⚡ [배전망/ESS 주요 뉴스 브리핑]\n\n{summary}"
        
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
