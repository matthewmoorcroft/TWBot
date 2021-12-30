import logging
import time
import re
from core.extractors import Extractor


class ResourceManager:
    actual = {}

    requested = {}

    storage = 0
    ratio = 2.5
    max_trade_amount = 4000
    logger = None
    # not allowed to bias
    trade_bias = 1
    last_trade = 0
    trade_max_per_hour = 1
    trade_max_duration = 2
    wrapper = None
    village_id = None
    do_premium_trade = False

    def __init__(self, wrapper=None, village_id=None):
        self.wrapper = wrapper
        self.village_id = village_id

    def update(self, game_state):
        self.actual["wood"] = game_state["village"]["wood"]
        self.actual["stone"] = game_state["village"]["stone"]
        self.actual["iron"] = game_state["village"]["iron"]
        self.actual["pop"] = (
            game_state["village"]["pop_max"] - game_state["village"]["pop"]
        )
        self.storage = game_state["village"]["storage_max"]
        self.check_state()
        self.logger = logging.getLogger(
            "Resource Manager: %s" % game_state["village"]["name"]
        )

    def do_premium_stuff(self):
        gpl = self.get_plenty_off()
        self.logger.debug(f"Trying premium trade: gpl {gpl} do? {self.do_premium_trade}")
        if gpl and self.do_premium_trade:
            url = "game.php?village=%s&screen=market&mode=exchange" % self.village_id
            res = self.wrapper.get_url(url=url)
            data = Extractor.premium_data(res.text)
            if not data:
                self.logger.warning("Error reading premium data!")
            price_fetch = ["wood", "stone", "iron"]
            prices = {}
            for p in price_fetch:
                prices[p] = data["stock"][p] * data["rates"][p]
            self.logger.info("Actual premium prices: %s" % prices)

            if gpl in prices and prices[gpl] * 1.1 < self.actual[gpl]:
                self.logger.info(
                    "Attempting trade of %d %s for premium point" % (prices[gpl], gpl)
                )
                self.wrapper.get_api_action(
                    self.village_id,
                    action="exchange_begin",
                    params={"screen": "market"},
                    data={"sell_%s" % gpl: "1"},
                )

    def check_state(self):
        for source in self.requested:
            for res in self.requested[source]:
                if self.requested[source][res] <= self.actual[res]:
                    self.requested[source][res] = 0

    def request(self, source="building", resource="wood", amount=1):
        if source in self.requested:
            self.requested[source][resource] = amount
        else:
            self.requested[source] = {resource: amount}

    def can_recruit(self):
        if self.actual["pop"] == 0:
            self.logger.info("Can't recruit, no room for pops!")
            for x in self.requested:
                if "recruitment" in x:
                    del self.requested[x]
            return False

        for x in self.requested:
            if "recruitment" in x:
                continue
            types = self.requested[x]
            for sub in types:
                if types[sub] > 0:
                    return False
        return True

    def get_plenty_off(self):
        most_of = 0
        most = None
        for sub in self.actual:
            f = 1
            for sr in self.requested:
                if sub in self.requested[sr] and self.requested[sr][sub] > 0:
                    f = 0
            if not f:
                continue
            if sub == "pop":
                continue
            # self.logger.debug(f"We have {self.actual[sub]} {sub}. Enough? {self.actual[sub]} > {int(self.storage / self.ratio)}")
            if self.actual[sub] > int(self.storage / self.ratio):
                if self.actual[sub] > most_of:
                    most = sub
                    most_of = self.actual[sub]
        # if most:
        #     self.logger.debug(f"We have plenty of {most}")

        return most

    def in_need_of(self, obj_type):
        for x in self.requested:
            types = self.requested[x]
            if obj_type in types and self.requested[x][obj_type] > 0:
                return True
        return False

    def in_need_amount(self, obj_type):
        amount = 0
        for x in self.requested:
            types = self.requested[x]
            if obj_type in types and self.requested[x][obj_type] > 0:
                amount += self.requested[x][obj_type]
        return amount

    def get_needs(self):
        needed_the_most = None
        needed_amount = 0
        for x in self.requested:
            types = self.requested[x]
            for obj_type in types:
                if (
                    self.requested[x][obj_type] > 0
                    and self.requested[x][obj_type] > needed_amount
                ):
                    needed_amount = self.requested[x][obj_type]
                    needed_the_most = obj_type
        if needed_the_most:
            return needed_the_most, needed_amount
        return None

    def trade(self, me_item, me_amount, get_item, get_amount):
        url = "game.php?village=%s&screen=market&mode=own_offer" % self.village_id
        res = self.wrapper.get_url(url=url)
        if 'market_merchant_available_count">0' in res.text:
            self.logger.debug("Not trading because not enough merchants available")
            return False
        payload = {
            "res_sell": me_item,
            "sell": me_amount,
            "res_buy": get_item,
            "buy": get_amount,
            "max_time": self.trade_max_duration,
            "multi": 1,
            "h": self.wrapper.last_h,
        }
        post_url = (
            "game.php?village=%s&screen=market&mode=own_offer&action=new_offer"
            % self.village_id
        )
        self.wrapper.post_url(post_url, data=payload)
        self.last_trade = int(time.time())
        return True

    def drop_existing_trades(self):
        url = "game.php?village=%s&screen=market&mode=all_own_offer" % self.village_id
        data = self.wrapper.get_url(url)
        existing = re.findall(r'data-id="(\d+)".+?data-village="(\d+)"', data.text)
        for entry in existing:
            offer, village = entry
            if village == str(self.village_id):
                post_url = (
                    "game.php?village=%s&screen=market&mode=all_own_offer&action=delete_offers"
                    % self.village_id
                )
                post = {
                    "id_%s" % offer: "on",
                    "delete": "Verwijderen",
                    "h": self.wrapper.last_h,
                }
                self.wrapper.post_url(url=post_url, data=post)
                self.logger.info(
                    "Removing offer %s from market because it existed too long" % offer
                )

    def manage_market(self, drop_existing=True):
        last = self.last_trade + int(3600 * self.trade_max_per_hour)
        if last > int(time.time()):
            self.logger.debug("Won't trade for %d seconds" % (last - int(time.time())))
            return

        get_h = time.localtime().tm_hour
        if get_h in range(0, 6) or get_h == 23:
            self.logger.debug("Not managing trades between 23h-6h")
            return
        if drop_existing:
            self.drop_existing_trades()
        plenty = self.get_plenty_off()
        if plenty and not self.in_need_of(plenty):
            need = self.get_needs()
            if need:
                item, how_many = need
                how_many = round(how_many, -1)
                if how_many < 250:
                    return
                
                self.logger.debug("Checking current market offers")
                if self.check_other_offers(item, how_many, plenty):
                    self.logger.debug("Took market offer!")
                    return

                if how_many > self.max_trade_amount:
                    how_many = self.max_trade_amount
                    self.logger.debug(
                        "Lowering trade amount of %d to %d because of limitation"
                        % (how_many, self.max_trade_amount)
                    )
                biased = int(how_many * self.trade_bias)
                if self.actual[plenty] < biased:
                    self.logger.debug("Cannot trade because insufficient resources")
                    return
                self.logger.info(
                    "Adding market trade of %d %s -> %d %s"
                    % (how_many, item, biased, plenty)
                )
                self.wrapper.reporter.report(
                    self.village_id,
                    "TWB_MARKET",
                    "Adding market trade of %d %s -> %d %s"
                    % (how_many, item, biased, plenty),
                )

                self.trade(plenty, biased, item, how_many)

    def check_other_offers(self, item, how_many, sell):
        url = "game.php?village=%s&screen=market&mode=other_offer" % self.village_id
        res = self.wrapper.get_url(url=url)
        p = re.compile(
            r"(?:<!-- insert the offer -->\n+)\s+<tr>(.*?)<\/tr>", re.S | re.M
        )
        cur_off_tds = p.findall(res.text)

        willing_to_sell = self.actual[sell] - self.in_need_amount(sell)
        self.logger.debug(f"Found {len(cur_off_tds)} offers on market, willing to sell {willing_to_sell} {sell}")


        for tds in cur_off_tds:
            res_offer = re.findall(
                r"<span class=\"icon header (.+?)\".+?>(.+?)</td>", tds
            )
            off_id = re.findall(
                r"<input type=\"hidden\" name=\"id\" value=\"(\d+)", tds
            )

            if len(off_id) < 1:
                # Not enough resources to trade
                continue

            offer = self.parse_res_offer(res_offer, off_id[0])
            if (
                offer["offered"] == item
                and offer["offer_amount"] >= how_many
                and offer["wanted"] == sell
                and offer["wanted_amount"] <= willing_to_sell
            ):
                self.logger.info(
                    f"Good offer: {offer['offer_amount']} {offer['offered']} for {offer['wanted_amount']} {offer['wanted']}"
                )
                # Take the deal!
                payload = {
                    "count": 1,
                    "id": offer["id"],
                    "h": self.wrapper.last_h,
                }
                post_url = f"game.php?village={self.village_id}&screen=market&mode=other_offer&action=accept_multi&start=0&id={offer['id']}&h={self.wrapper.last_h}"
                # print(f"Would post: {post_url} {payload}")
                self.wrapper.post_url(post_url, data=payload)
                self.last_trade = int(time.time())
                self.actual[offer['wanted']] = self.actual[offer['wanted']] - offer['wanted_amount']
                return True

        # No useful offers found
        return False

    def parse_res_offer(self, res_offer, id):
        off, want, ratio = res_offer
        res_offer, res_offer_amount = off
        res_wanted, res_wanted_amount = want

        return {
            "id": id,
            "offered": res_offer,
            "offer_amount": int("".join([s for s in res_offer_amount if s.isdigit()])),
            "wanted": res_wanted,
            "wanted_amount": int(
                "".join([s for s in res_wanted_amount if s.isdigit()])
            ),
        }