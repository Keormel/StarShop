from aiohttp import web

async def crystalpay_webhook(request):
    data = await request.json()
    if data.get("status") == "paid":
        purchase_id = data.get("order_id")
        # Здесь можно обновить статус покупки в базе данных
        return web.json_response({"status": "ok"})

app = web.Application()
app.router.add_post("/webhook", crystalpay_webhook)

if __name__ == "__main__":
    web.run_app(app, port=8000)
