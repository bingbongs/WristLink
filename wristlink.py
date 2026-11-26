"""WristLink desktop application for Windows."""
import asyncio
import contextlib
import json
import os
import queue
import tempfile
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, ttk

from bridge import (DEFAULT_IDLE, DEFAULT_PROFILE, HR_SERVICE, IDLE_MODES, INTERVALS, DutyCycle, Output,
                    find_bands, possible_brookstone, stream, vibration)
from history import History, discharge, profile_estimates
from plot import BG, DIM, EDGE, FONT, GOLD, MUTED, PANEL, TEXT, TimePlot

DATA = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "WristLink"
PREFS = DATA / "settings.json"
RANGES = {"15 min":900, "1 hour":3600, "24 hours":86400, "7 days":604800}


def load_settings():
    try:
        data = json.loads(PREFS.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    PREFS.parent.mkdir(exist_ok=True, parents=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=PREFS.parent, delete=False, encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    try:
        os.replace(f.name, PREFS)
    finally:
        Path(f.name).unlink(missing_ok=True)


def label(parent, text="", size=10, color=TEXT, bold=False, **opts):
    return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color, anchor="w",
                    font=(FONT+" Semibold" if bold else FONT, size), **opts)


def button(parent, text, command, *, accent=False, quiet=False):
    bg = GOLD if accent else (parent.cget("bg") if quiet else "#28292b")
    fg = BG if accent else TEXT
    return tk.Button(parent,text=text,command=command,bg=bg,fg=fg,activebackground="#dfbf83" if accent else "#373839",
                     activeforeground=fg,relief="flat",bd=0,highlightthickness=0,padx=8,pady=3,
                     font=(FONT+" Semibold",10),cursor="hand2",disabledforeground=DIM)


def card(parent):
    return tk.Frame(parent,bg=PANEL,highlightthickness=1,highlightbackground=EDGE)


def style_window(win):
    from ctypes import WinDLL, byref, c_int, c_void_p, sizeof
    icon=Path(__file__).with_name("wristlink.ico")
    if icon.is_file():
        win.iconbitmap(str(icon))
    def apply():
        if not win.winfo_exists(): return
        hwnd=win.wm_frame()
        with contextlib.suppress(OSError, ValueError):
            dark=c_int(1)
            WinDLL("dwmapi").DwmSetWindowAttribute(c_void_p(int(hwnd,0)),20,byref(dark),sizeof(dark))
    win.after_idle(apply)


def duration(hours):
    return f"{hours/24:.1f} days" if hours>=48 else f"{hours:.0f} hours"


def next_alarm(clock, now=None):
    now = now or datetime.now()
    t = datetime.strptime(clock.strip(), "%H:%M").time()
    target = datetime.combine(now.date(), t)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


class Window:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.history = History(DATA / "history.sqlite3")
        self.address = self.settings.get("address", "")
        self.name = self.settings.get("name") or "No sensor selected"
        self.verified = self.settings.get("model") == "LA380"
        defaults = asdict(Output())
        saved_output = self.settings.get("output", {})
        if isinstance(saved_output,dict):
            defaults.update({k:v for k,v in saved_output.items() if k in defaults})
        self.output = Output(**defaults)
        self.profile = self.settings.get("profile", DEFAULT_PROFILE)
        self.idle = self.settings.get("idle", DEFAULT_IDLE)
        if self.profile not in INTERVALS: self.profile=DEFAULT_PROFILE
        if self.idle not in IDLE_MODES: self.idle=DEFAULT_IDLE
        self.aliases = self.settings.get("aliases", True)
        self.confirm_jumps = self.settings.get("confirm_jumps", True)
        self.events = queue.SimpleQueue()
        self.lock = threading.Lock()
        self.loop = self.task = self.running = self.after_job = None
        self.cancelled = self.closing = False
        self.armed = True
        self.cycle = DutyCycle()
        self.cycle.seen.extend(self.history.recent_band_stamps(self.address, time.time()))
        self.check_popup = None
        self.retry_scan_at = 0
        self.scan_delay = 12
        self.sensor_popup = self.settings_popup = None
        self.rows = {}
        self.tab = "Timeline"
        saved_range = self.settings.get("range")
        self.range = tk.StringVar(value=saved_range if saved_range in RANGES else "1 hour")
        self.mini = self.settings.get("mini") is True
        self.fresh = False
        self.rechecking = False
        self.offset = 0
        self.anchor_time = None
        self.state = tk.StringVar(value="Starting")
        self.status = tk.StringVar()
        self.last_read = self.battery = self.next_time = None
        self.link_since = self.sample_gap = None
        self.refresh_at = 0
        self.notice_until = 0
        self.alert_queue = deque()
        self.make_widgets()
        self.restore_display()
        self.show_tab("Timeline")
        self.apply_size()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.pump)
        self.root.after(180, self.begin if self.address else self.scan)

    def make_widgets(self):
        r=self.root
        r.title("WristLink")
        r.configure(bg=BG)
        style_window(r)
        style=ttk.Style(r)
        style.theme_use("clam")
        style.configure("TCombobox",padding=5,fieldbackground="#28292b",background="#28292b",
                        bordercolor=EDGE,arrowcolor=GOLD,foreground=TEXT,lightcolor=EDGE,darkcolor=EDGE)
        style.map("TCombobox",fieldbackground=[("readonly","#28292b")],foreground=[("readonly",TEXT)],
                  selectbackground=[("readonly","#28292b")],selectforeground=[("readonly",TEXT)])
        r.option_add("*TCombobox*Listbox.background","#28292b")
        r.option_add("*TCombobox*Listbox.foreground",TEXT)
        r.option_add("*TCombobox*Listbox.selectBackground","#675635")
        top=tk.Frame(r,bg=BG,padx=12,pady=4)
        top.pack(fill="x")
        mark=tk.Canvas(top,width=32,height=32,bg=BG,highlightthickness=0)
        mark.pack(side="left",padx=(0,9))
        mark.create_line(0,18,7,18,11,8,17,26,22,13,25,18,32,18,fill=GOLD,width=2.6,joinstyle="round")
        label(top,"WristLink",15,bold=True).pack(side="left")
        button(top,"Settings",self.preferences).pack(side="right")
        self.size_button=button(top,"Mini",self.toggle_size,quiet=True)
        self.size_button.pack(side="right",padx=4)
        tk.Frame(r,bg=EDGE,height=1).pack(fill="x")
        shell=tk.Frame(r,bg=BG,padx=10,pady=6)
        shell.pack(fill="both",expand=True)
        self.head=card(shell)
        self.head.pack(fill="x")
        hero=tk.Frame(self.head,bg=PANEL,padx=10,pady=4)
        hero.pack(fill="x")
        left=tk.Frame(hero,bg=PANEL)
        left.pack(side="left")
        self.bpm_title=label(left,"Heart rate",9,MUTED)
        self.bpm_title.pack(anchor="w")
        numbers=tk.Frame(left,bg=PANEL)
        numbers.pack(anchor="w")
        self.bpm=label(numbers,"—",25,TEXT,True)
        self.bpm.pack(side="left")
        label(numbers,"BPM",10,GOLD,True).pack(side="left",anchor="s",pady=9,padx=6)
        self.age=label(left,"",9,MUTED)
        self.age.pack(anchor="w")
        metrics=tk.Frame(hero,bg=PANEL)
        metrics.pack(side="right",anchor="center")
        self.charge=label(metrics,"—",15,TEXT,True)
        self.charge.grid(row=1,column=0,sticky="w",padx=(0,15))
        label(metrics,"Battery",9,MUTED).grid(row=0,column=0,sticky="w")
        self.link_label=label(metrics,"Next check",9,MUTED)
        self.link_label.grid(row=0,column=1,sticky="w")
        self.countdown=label(metrics,"—",15,GOLD,True,width=5)
        self.countdown.grid(row=1,column=1,sticky="w")
        nav=self.nav_frame=tk.Frame(shell,bg=BG)
        nav.pack(fill="x",pady=4)
        self.nav={}
        for title in ("Timeline","Battery","Reminders"):
            b=button(nav,title,lambda t=title:self.show_tab(t),quiet=True)
            b.pack(side="left",padx=(0,4))
            self.nav[title]=b
        button(nav,"Check pulse",self.check_pulse,quiet=True).pack(side="right")
        self.body=card(shell)
        self.body.pack(fill="both",expand=True)
        footer=self.footer=tk.Frame(shell,bg=BG)
        footer.pack(before=nav,side="bottom",fill="x",pady=(4,0))
        self.play=button(footer,"Pause",self.toggle)
        self.play.pack(side="right")
        button(footer,"Sensors",lambda:self.change(self.manual_scan),quiet=True).pack(side="right",padx=6)
        self.state_label=label(footer,textvariable=self.state,size=9,color=GOLD,bold=True)
        self.state_label.pack(anchor="w")
        self.detail=label(footer,textvariable=self.status,size=9,color=MUTED)
        self.detail.pack(anchor="w")

    def apply_size(self, reposition=True):
        r = self.root
        if self.mini:
            self.nav_frame.pack_forget()
            self.body.pack_forget()
        else:
            self.nav_frame.pack(fill="x", pady=4)
            self.body.pack(fill="both", expand=True)
        self.size_button.configure(text="Expand" if self.mini else "Mini")
        # Scale the layout with the system text metrics on high-DPI displays.
        scale = max(1, float(r.tk.call("tk","scaling")) / (96/72))
        available = (r.winfo_screenwidth()-20, r.winfo_screenheight()-72)
        target = (382,206) if self.mini else (502,454)
        width,height = [min(round(v*scale),limit) for v,limit in zip(target,available)]
        x = (available[0]-width)//2 if reposition else r.winfo_x()
        y = (available[1]-height)//2 if reposition else r.winfo_y()
        r.minsize(width,height)
        r.geometry(f"{width}x{height}+{max(0,min(x,available[0]-width))}+{max(0,min(y,available[1]-height))}")

    def toggle_size(self):
        self.mini = not self.mini
        self.apply_size(False)
        self.persist()

    def persist(self):
        self.settings.update(address=self.address,name=self.name,model="LA380" if self.verified else "",
                             output=asdict(self.output),profile=self.profile,idle=self.idle,aliases=self.aliases,
                             mini=self.mini,range=self.range.get(),confirm_jumps=self.confirm_jumps)
        try:
            save_settings(self.settings)
        except OSError as exc:
            self.message(f"Could not save settings · {exc}")

    def message(self, value, seconds=10):
        self.notice_until=time.time()+seconds
        self.status.set(value)

    def restore_display(self):
        self.fresh=False
        self.last_read=self.history.latest(self.address) if self.address else None
        latest=self.history.db.execute("SELECT level FROM battery WHERE device=? ORDER BY ts DESC LIMIT 1",
                                       (self.address,)).fetchone()
        self.battery=latest[0] if latest else None
        self.bpm.configure(text=str(self.last_read[1]) if self.last_read else "—")
        self.update_footer()

    def update_footer(self):
        title="Band estimate" if self.fresh or not self.last_read else "Last recorded"
        self.bpm_title.configure(text="Band · OSC held" if self.fresh and self.rechecking else title)
        self.bpm.configure(fg=TEXT if self.fresh else MUTED)
        self.charge.configure(text=f"{self.battery}%" if self.battery is not None else "—")
        remaining=max(0,int(self.next_time-time.time()+0.99)) if self.armed and self.next_time else None
        continuous=self.profile==DEFAULT_PROFILE
        self.link_label.configure(text="Bluetooth" if continuous else "Next check")
        countdown=f"{remaining//60}:{remaining%60:02d}" if remaining is not None else "—"
        if continuous:
            countdown="Up" if self.link_since else "—"
        self.countdown.configure(text=countdown)
        if self.last_read:
            age=max(0,int(time.time()-self.last_read[0]))
            stamp=datetime.fromtimestamp(self.last_read[0]).strftime("%H:%M:%S")
            self.age.configure(text=f"{stamp}   ·   "+("Just received" if age<2 else f"{age}s ago" if age<60 else
                               f"{age//60}m ago" if age<3600 else f"{age//3600}h ago"))
        else:
            self.age.configure(text="Waiting for first reading")
        if time.time()>=self.notice_until:
            if self.link_since and continuous:
                elapsed=max(0,int(time.time()-self.link_since))
                detail=f"Link {elapsed//60}:{elapsed%60:02d}"
                if self.sample_gap is not None:
                    detail+=f" · last samples {self.sample_gap:.1f}s apart"
                self.status.set(detail)
            else:
                self.status.set(f"{self.name} · {self.profile.split(' · ')[0]}" if self.address else
                                "Wake the band and keep it near the PC.")

    def show_tab(self, title):
        for child in self.body.winfo_children(): child.destroy()
        self.tab=title
        for name,b in self.nav.items():
            b.configure(bg="#302c23" if name==title else BG,fg=GOLD if name==title else MUTED)
        page=tk.Frame(self.body,bg=PANEL,padx=9,pady=5)
        page.pack(fill="both",expand=True)
        if title=="Reminders":
            self.reminder_page(page)
            return
        header=tk.Frame(page,bg=PANEL)
        page.columnconfigure(0,weight=1)
        page.rowconfigure(2,weight=1)
        header.grid(row=0,column=0,sticky="ew")
        label(header,"Heart-rate history" if title=="Timeline" else "Battery history",12,bold=True).pack(side="left")
        self.range_box=ttk.Combobox(header,values=tuple(RANGES),textvariable=self.range,state="readonly",width=8)
        self.range_box.pack(side="right")
        self.range_box.bind("<<ComboboxSelected>>",self.change_range)
        self.forward=button(header,"›",lambda:self.jump(1),quiet=True)
        self.forward.pack(side="right")
        button(header,"‹",lambda:self.jump(-1),quiet=True).pack(side="right")
        self.period=label(page,"",9,DIM)
        self.period.grid(row=1,column=0,sticky="w")
        self.chart=TimePlot(page)
        self.chart.grid(row=2,column=0,sticky="nsew")
        tk.Frame(page,bg=EDGE,height=1).grid(row=3,column=0,sticky="ew")
        bottom=tk.Frame(page,bg=PANEL)
        bottom.grid(row=4,column=0,sticky="ew",pady=(2,0))
        if title=="Timeline":
            button(bottom,"Export CSV",self.export,quiet=True).pack(side="right")
        self.stats=label(bottom,"",9,TEXT,wraplength=405)
        self.stats.pack(side="left")
        self.learning=label(page,"",9,MUTED,wraplength=440)
        if title=="Battery":
            self.learning.grid(row=5,column=0,sticky="w")
            tiles=tk.Frame(page,bg=PANEL)
            tiles.grid(row=6,column=0,sticky="ew",pady=(3,0))
            self.budgets={}
            for i,(title,seconds) in enumerate(INTERVALS.items()):
                tiles.columnconfigure(i,weight=1,uniform="budget")
                tile=tk.Frame(tiles,bg="#242527",padx=8,pady=3)
                tile.grid(row=0,column=i,sticky="ew",padx=(0,4))
                label(tile,title.split(" · ")[0],9,GOLD,True).pack(anchor="w")
                self.budgets[seconds]=label(tile,"",9,MUTED)
                self.budgets[seconds].pack(anchor="w")
        self.refresh()

    def change_range(self, event):
        self.jump(0)
        self.persist()

    def jump(self, step):
        if step==0:
            self.offset=0
        else:
            if self.offset==0: self.anchor_time=time.time()
            self.offset=min(0,self.offset+step)
        self.refresh()

    def refresh(self):
        self.refresh_at=time.time()+25
        if self.tab=="Reminders": return
        span=RANGES[self.range.get()]
        end=(self.anchor_time or time.time())+self.offset*span if self.offset else time.time()
        start=end-span
        battery=self.tab=="Battery"
        data, (count,avg,low,high)=self.history.plot(self.address,start,end,battery)
        self.chart.show(data,start,end,battery)
        self.period.configure(text=f"{datetime.fromtimestamp(start):%b %d, %H:%M}  —  {datetime.fromtimestamp(end):%b %d, %H:%M}")
        self.forward.configure(state="normal" if self.offset else "disabled")
        if not battery:
            self.stats.configure(text=(f"{count:,} readings     ·     Average {avg:.0f} BPM     ·     Range {low}–{high} BPM"
                                       if count else "0 readings"))
            return
        now=time.time()
        points=self.history.batteries(self.address,now-7*86400,now)
        estimate=discharge(points)
        projections=profile_estimates(points)
        for seconds,tile in self.budgets.items():
            runtime=duration(projections[seconds])+"/charge" if seconds in projections else "Learning"
            tile.configure(text=runtime if seconds==1 else f"{3600//seconds}/h · {runtime}")
        requests,samples,seconds=self.history.stats(self.address,now)
        text=f"Past 24h   ·   {requests} optical requests   ·   {samples} readings"
        if seconds is not None: text+=f"   ·   {seconds:.1f}s mean acquisition"
        self.stats.configure(text=text)
        if estimate and now-points[-1][0]<2*3600:
            rate,remaining,full=estimate
            message=f"Recent drain {rate:.2f}%/h   ·   Approx. {duration(remaining)} remaining   ·   {duration(full)} per full charge at this usage."
        else:
            message="Learning runtime · needs 6h, a 10% drop and 3 charge levels."
        self.learning.configure(text=message)

    def export(self):
        if not self.address: return self.message("Select a sensor before exporting.")
        path=filedialog.asksaveasfilename(parent=self.root,defaultextension=".csv",initialfile="WristLink history.csv",
                                          filetypes=[("CSV files","*.csv")])
        if path:
            try:
                self.history.export(self.address,path)
                self.message("Sensor history exported.")
            except OSError as exc:
                self.message(str(exc))

    def launch(self, kind, operation):
        self.running=kind
        self.cancelled=False
        def worker():
            async def run():
                with self.lock:
                    self.loop=asyncio.get_running_loop()
                    self.task=asyncio.current_task()
                    if self.cancelled: return
                await operation()
            try:
                asyncio.run(run())
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.events.put(("error",str(exc) or type(exc).__name__))
            finally:
                with self.lock: self.loop=self.task=None
                self.events.put(("done",kind))
        threading.Thread(target=worker,daemon=True).start()

    def change(self, fn):
        self.retry_scan_at=0
        if self.running=="stream":
            self.fresh=False
        if not self.running:
            if fn: fn()
            return
        self.after_job=fn
        with self.lock:
            self.cancelled=True
            if self.loop and self.task:
                with contextlib.suppress(RuntimeError):
                    self.loop.call_soon_threadsafe(self.task.cancel)

    def scan(self):
        self.state.set("Finding sensor")
        async def operation():
            self.events.put(("devices",await find_bands()))
        self.launch("scan",operation)

    def manual_scan(self):
        self.open_picker()
        self.scan()

    def begin(self):
        if not self.armed: return
        if not self.address: return self.scan()
        addr=self.address
        self.next_time=None
        self.state.set("Starting")
        self.launch("stream",lambda:stream(addr,self.output,lambda k,v:self.events.put((k,v)),asyncio.Event(),
                    profile=self.profile,idle=self.idle,aliases=self.aliases,verified=self.verified,cycle=self.cycle,
                    confirm_jumps=self.confirm_jumps))

    def choose(self, addr, name, verified):
        if self.address!=addr:
            self.cycle=DutyCycle()
            self.cycle.seen.extend(self.history.recent_band_stamps(addr,time.time()))
        self.address,self.name,self.verified=addr,name,verified
        self.armed=True
        self.play.configure(text="Pause")
        self.persist()
        self.restore_display()
        self.refresh()
        self.begin()

    def toggle(self):
        self.armed=not self.armed
        self.fresh=False
        self.next_time=None
        self.play.configure(text="Pause" if self.armed else "Resume",bg="#28292b" if self.armed else GOLD,
                            fg=TEXT if self.armed else BG)
        self.state.set("Starting" if self.armed else "Paused")
        self.change(self.begin if self.armed else None)

    def popup(self, title):
        win=tk.Toplevel(self.root,bg=PANEL,padx=20,pady=16)
        win.title(title)
        win.transient(self.root)
        win.resizable(False,False)
        win.geometry(f"+{self.root.winfo_rootx()+55}+{self.root.winfo_rooty()+60}")
        style_window(win)
        return win

    def check_pulse(self):
        if self.check_popup and self.check_popup.winfo_exists():
            self.check_popup.lift()
            return
        win=self.check_popup=self.popup("Compare your pulse")
        label(win,"Compare your pulse",15,bold=True).pack(anchor="w")
        label(win,"Sit still with the band on. When you press Start, count your pulse for the full minute. WristLink will request readings during the count.",
              10,MUTED,wraplength=350,justify="left").pack(anchor="w",pady=(3,6))
        status=label(win,"Ready",11,GOLD,True,wraplength=350,justify="left")
        status.pack(anchor="w",pady=4)
        row=tk.Frame(win,bg=PANEL)
        row.pack(fill="x",pady=4)
        label(row,"Beats counted",10).pack(side="left")
        entry=tk.Entry(row,width=6,font=(FONT,11),justify="center",state="disabled")
        entry.pack(side="left",padx=8)
        start_time=deadline=None
        checked_device=cycle=None
        data=[]
        middle=None

        def tick():
            nonlocal data,middle
            if not win.winfo_exists(): return
            if self.address!=checked_device or not self.armed or self.cycle is not cycle:
                cycle.check_until=0
                status.configure(text="Check interrupted. Start again when the band is ready.")
                start_button.configure(state="normal")
                return
            left=deadline-time.monotonic()
            if left>0:
                status.configure(text=f"Count now · {int(left+0.999)}s left")
                win.after(150,tick)
                return
            data,middle=self.history.pulse_check(checked_device,start_time,start_time+60)
            start_button.configure(state="normal",text="Start again")
            if len(data)<2:
                status.configure(text=f"Count finished · {len(data)} band readings. At least two are needed to compare. Try again with the band nearby.")
                return
            status.configure(text=f"Count finished · {len(data)} band readings. Enter your beat count.")
            entry.configure(state="normal")
            compare.configure(state="normal")
            entry.focus_set()

        def start():
            nonlocal start_time,deadline,checked_device,cycle
            if not self.address or not self.armed or self.running!="stream":
                status.configure(text="Start sampling with your sensor first.")
                return
            checked_device,cycle=self.address,self.cycle
            start_time=time.time()
            deadline=time.monotonic()+60
            cycle.check_until=deadline
            entry.configure(state="normal")
            entry.delete(0,"end")
            entry.configure(state="disabled")
            compare.configure(state="disabled")
            start_button.configure(state="disabled")
            tick()

        def show_result():
            try:
                reference=int(entry.get())
                if reference<=0: raise ValueError
            except ValueError:
                status.configure(text="Enter the number of beats you counted in 60 seconds.")
                return
            status.configure(text=f"Your count {reference} BPM · band median {middle:g} BPM\n"
                             f"Band minus count {middle-reference:+g} BPM\n"
                             f"Band samples ({len(data)}) · {min(data)}–{max(data)} BPM")

        compare=button(row,"Compare",show_result)
        compare.configure(state="disabled")
        compare.pack(side="right")
        start_button=button(win,"Start 60 seconds",start,accent=True)
        start_button.pack(anchor="w",pady=(4,0))

        def close():
            if cycle is not None: cycle.check_until=0
            win.destroy()
        win.protocol("WM_DELETE_WINDOW",close)

    def open_picker(self):
        if self.sensor_popup and self.sensor_popup.winfo_exists():
            self.sensor_popup.lift()
            return
        win=self.sensor_popup=self.popup("Bluetooth sensors")
        label(win,"Bluetooth sensors",15,bold=True).pack(anchor="w")
        self.pick_help=label(win,"Looking for nearby bands…",10,MUTED)
        self.pick_help.pack(anchor="w",pady=(4,8))
        self.pick=ttk.Combobox(win,state="readonly",width=47)
        self.pick.pack(fill="x",pady=5)
        actions=tk.Frame(win,bg=PANEL)
        actions.pack(fill="x",pady=(8,0))
        def select():
            row=self.rows.get(self.pick.get())
            if not row: return
            win.destroy()
            self.change(lambda:self.choose(*row))
        button(actions,"Scan again",lambda:self.change(self.scan),quiet=True).pack(side="left")
        button(actions,"Use sensor",select,accent=True).pack(side="right")
        def dismiss():
            win.destroy()
            self.change(self.begin if self.armed else None)
        win.protocol("WM_DELETE_WINDOW",dismiss)

    def populate(self, result):
        devices,confirmed,unresolved=result
        self.rows={}
        for dev,ad in devices:
            certain=dev.address in confirmed
            name="Brookstone LA380" if certain else (ad.local_name if ad else None) or dev.name or (
                "Possible FitPro band" if possible_brookstone(dev,ad) else "Unnamed device")
            suffix="  ·  Heart Rate Service" if ad and HR_SERVICE in ad.service_uuids else ""
            signal=f"{ad.rssi} dBm" if ad else "Windows paired"
            self.rows[f"{name}{suffix}  ·  {dev.address}  ·  {signal}"]=(dev.address,name,certain)
        picker_open=self.sensor_popup and self.sensor_popup.winfo_exists()
        if not picker_open and len(confirmed)==1 and not unresolved:
            self.scan_delay=12
            self.after_job=lambda:self.choose(confirmed[0],"Brookstone LA380",True)
            return
        if not picker_open and (len(confirmed)>1 or (confirmed and unresolved)):
            self.open_picker()
            picker_open=True
        if picker_open:
            self.pick.configure(values=tuple(self.rows))
            self.pick.set("")
            self.pick_help.configure(text="Choose your sensor by its signal or address." if self.rows else "No devices yet. Wake your band and scan again.")
            self.state.set("Select sensor")
        else:
            self.state.set("Waiting for band")
            self.retry_scan_at=time.time()+self.scan_delay
            self.scan_delay=min(60,self.scan_delay*2)

    def preferences(self):
        if self.settings_popup and self.settings_popup.winfo_exists():
            self.settings_popup.lift()
            return
        win=self.settings_popup=self.popup("WristLink settings")
        label(win,"Settings",17,bold=True).grid(row=0,column=0,columnspan=2,sticky="w",pady=(0,10))
        fields={}
        def field(row,title,value,choices=None):
            label(win,title,10,MUTED).grid(row=row,column=0,sticky="w",padx=(0,18),pady=4)
            var=tk.StringVar(value=value)
            fields[title]=var
            if choices:
                control=ttk.Combobox(win,textvariable=var,values=choices,state="readonly",width=25)
            else:
                control=tk.Entry(win,textvariable=var,bg="#28292b",fg=TEXT,insertbackground=GOLD,
                                 relief="flat",width=28,font=(FONT,10))
            control.grid(row=row,column=1,sticky="ew",ipady=3)
        field(1,"In-session sampling",self.profile,tuple(INTERVALS))
        field(2,"Outside local OSC",self.idle,IDLE_MODES)
        hint=label(win,"",9,GOLD,wraplength=425)
        hint.grid(row=3,column=0,columnspan=2,sticky="w",pady=(3,12))
        def budget(*_):
            mode=fields["Outside local OSC"].get()
            active=3600//INTERVALS[fields["In-session sampling"].get()]
            idle=0 if mode==IDLE_MODES[1] else 4 if mode==IDLE_MODES[0] else active
            if active==3600:
                hint.configure(text="Keep Bluetooth connected. Target one second between starts, waiting for each optical result.")
            else:
                hint.configure(text=f"Up to {active} optical checks/hour in session and {idle}/hour outside local OSC.")
        fields["In-session sampling"].trace_add("write",budget)
        fields["Outside local OSC"].trace_add("write",budget)
        budget()
        titles={"host":"OSC destination","port":"UDP port","parameter":"BPM parameter","mode":"Value type","active":"Signal parameter"}
        for row,(key,value) in enumerate(asdict(self.output).items(),4):
            field(row,titles[key],value,("Int","Float") if key=="mode" else None)
        alias=tk.BooleanVar(value=self.aliases)
        tk.Checkbutton(win,text="Send common avatar BPM parameters",variable=alias,bg=PANEL,fg=TEXT,
                       selectcolor=BG,activebackground=PANEL,activeforeground=GOLD,font=(FONT,10)).grid(
                       row=9,column=0,columnspan=2,sticky="w",pady=6)
        note=label(win,"",9,MUTED,wraplength=430)
        confirm=tk.BooleanVar(value=self.confirm_jumps)
        tk.Checkbutton(win,text="Confirm abrupt LA380 changes before sending to OSC",variable=confirm,bg=PANEL,
                       fg=TEXT,selectcolor=BG,activebackground=PANEL,activeforeground=GOLD,font=(FONT,10)).grid(
                       row=10,column=0,columnspan=2,sticky="w")
        note.grid(row=11,column=0,columnspan=2,sticky="w")
        def apply():
            try:
                args={k:fields[titles[k]].get().strip() for k in asdict(self.output)}
                args["port"]=int(args["port"])
                out=Output(**args)
                out.validate()
            except ValueError as exc:
                note.configure(text=str(exc))
                return
            current=(self.output,self.aliases,self.confirm_jumps,self.profile,self.idle)
            self.output,self.aliases=out,alias.get()
            self.confirm_jumps=confirm.get()
            self.profile,self.idle=fields["In-session sampling"].get(),fields["Outside local OSC"].get()
            self.persist()
            win.destroy()
            if current!=(self.output,self.aliases,self.confirm_jumps,self.profile,self.idle):
                self.change(self.begin if self.armed else None)
        button(win,"Save settings",apply,accent=True).grid(row=12,column=1,sticky="e",pady=(8,0))

    def reminder_page(self,page):
        header=tk.Frame(page,bg=PANEL)
        header.pack(fill="x")
        label(header,"Wrist reminders",12,bold=True).pack(side="left")
        button(header,"Test vibration",lambda:self.buzz(self.address,"Test"),quiet=True).pack(side="right")
        label(page,"Keep the PC awake and WristLink open for wrist alerts.",9,MUTED).pack(anchor="w",pady=(2,6))
        def row():
            line=tk.Frame(page,bg=PANEL)
            line.pack(fill="x",pady=4)
            return line
        def entry(p,value):
            v=tk.StringVar(value=value)
            tk.Entry(p,width=7,textvariable=v,bg="#28292b",fg=TEXT,insertbackground=GOLD,
                     font=(FONT,11),relief="flat",justify="center").pack(side="left",ipady=5,padx=8)
            return v
        def scheduled(line,key):
            item=self.settings.get(key)
            if not item: return False
            when=datetime.fromtimestamp(item["due"]).strftime("%b %d, %H:%M")
            label(line,when,10,GOLD).pack(side="left")
            button(line,"Cancel",lambda:self.clear_alert(key),quiet=True).pack(side="right")
            return True
        timer_line=row()
        label(timer_line,"Timer",11,bold=True,width=12).pack(side="left")
        def set_timer():
            try:
                m=int(minutes.get())
                if not 1<=m<=1440: raise ValueError
            except ValueError:
                return self.message("Use a timer from 1 to 1440 minutes.")
            self.set_alert("timer",time.time()+60*m)
        if not scheduled(timer_line,"timer"):
            minutes=entry(timer_line,"10")
            label(timer_line,"min",9,MUTED).pack(side="left")
            button(timer_line,"Start timer",set_timer).pack(side="right")
        alarm_line=row()
        label(alarm_line,"Daily bedtime",11,bold=True,width=12).pack(side="left")
        def set_bedtime():
            try: due=next_alarm(bedtime.get())
            except ValueError: return self.message("Use a 24-hour time such as 22:30.")
            self.set_alert("bedtime",due,bedtime.get().strip())
        if not scheduled(alarm_line,"bedtime"):
            bedtime=entry(alarm_line,self.settings.get("bedtime",{}).get("clock","22:30"))
            label(alarm_line,"local time",9,MUTED).pack(side="left")
            button(alarm_line,"Set bedtime",set_bedtime).pack(side="right")

    def set_alert(self,key,due,clock=""):
        if not self.address: return self.message("Select your Brookstone band first.")
        self.settings[key]={"due":due,"address":self.address,"clock":clock}
        self.persist()
        self.show_tab("Reminders")

    def clear_alert(self,key):
        self.settings.pop(key,None)
        self.persist()
        self.show_tab("Reminders")

    def buzz(self,address,title):
        if not address: return self.message("Select your Brookstone band first.")
        if self.running=="stream" and address==self.address and self.profile==DEFAULT_PROFILE:
            with self.lock:
                alert=self.cycle.queue_alert
                if self.loop and alert:
                    try:
                        self.loop.call_soon_threadsafe(alert,title)
                        return
                    except RuntimeError:
                        pass
        if self.running=="buzz":
            self.alert_queue.append((address,title))
            return
        def start():
            self.state.set("Sending wrist alert")
            async def operation():
                await vibration(address)
                self.events.put(("notice",title+" · Band acknowledged the vibration command."))
            self.launch("buzz",operation)
        self.change(start)

    def check_alerts(self,now):
        for key in ("timer","bedtime"):
            item=self.settings.get(key)
            if not item or now<item["due"]: continue
            due=item["due"]
            if key=="bedtime": item["due"]=next_alarm(item["clock"])
            else: self.settings.pop(key,None)
            self.persist()
            if self.tab=="Reminders": self.show_tab("Reminders")
            if now-due<=120:
                self.buzz(item["address"],"Timer" if key=="timer" else "Bedtime")
                return
            self.message("A reminder expired while WristLink or the PC was asleep.")

    def close(self):
        self.closing=True
        self.armed=False
        self.change(self.finish)

    def finish(self):
        self.history.db.close()
        self.root.destroy()

    def pump(self):
        while not self.events.empty():
            kind,value=self.events.get()
            if kind=="done":
                self.running=None
                follow,self.after_job=self.after_job,None
                if self.closing:
                    self.finish()
                    return
                if follow: follow()
                elif value=="buzz":
                    if self.alert_queue: self.buzz(*self.alert_queue.popleft())
                    elif self.armed: self.begin()
                    else: self.state.set("Paused")
            elif kind=="devices":
                if not self.cancelled: self.populate(value)
            elif kind in ("reading","identity","request","link"):
                try: self.history.write(self.address,kind,value)
                except Exception as exc: self.message(f"History write failed · {exc}")
                if kind=="reading":
                    if self.link_since and self.last_read and self.last_read[0]>=self.link_since:
                        self.sample_gap=value[0]-self.last_read[0]
                    self.fresh=self.armed and not self.cancelled
                    self.rechecking=len(value)>=6 and value[5]=="held"
                    self.last_read=value[:3]
                    self.bpm.configure(text=str(value[1]))
                elif kind=="link":
                    stamp,event,detail=value
                    if event=="connected":
                        self.link_since=stamp
                        self.sample_gap=None
                    elif event in ("link_lost","session_ended"):
                        self.link_since=self.sample_gap=None
                elif kind=="identity":
                    name,protocol,level,_,_=value
                    if level is not None:
                        self.battery=level
                    name="Brookstone LA380" if self.verified else name
                    if name!=self.name:
                        self.name=name
                        self.persist()
                self.refresh_at=0
            elif kind=="signal":
                self.fresh=False
            elif kind=="model":
                self.verified=value=="LA380"
                self.persist()
            elif kind=="state":
                if not self.cancelled: self.state.set(value)
                if value not in ("Band resting","Background sampling") and not value.endswith(" · retrying"):
                    self.next_time=None
            elif kind=="next": self.next_time=value
            elif kind=="error":
                self.fresh=False
                self.state.set("Needs attention")
                self.message(value,45)
                if self.running=="scan" and self.armed: self.retry_scan_at=time.time()+25
            elif kind=="notice": self.message(value)
        now=time.time()
        if self.retry_scan_at and now>=self.retry_scan_at and not self.running and self.armed:
            self.retry_scan_at=0
            self.scan()
        if now>=self.refresh_at and self.tab!="Reminders": self.refresh()
        self.check_alerts(now)
        self.update_footer()
        self.root.after(200,self.pump)


def main():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    mutex = kernel.CreateMutexW(None, False, "Local\\WristLink_Bluetooth_OSC")
    if mutex and ctypes.get_last_error() == 183:
        user = ctypes.windll.user32
        user.FindWindowW.restype = wintypes.HWND
        hwnd = user.FindWindowW("TkTopLevel", "WristLink")
        if hwnd:
            user.ShowWindow(wintypes.HWND(hwnd), 9)
            user.SetForegroundWindow(wintypes.HWND(hwnd))
        return
    root = tk.Tk()
    Window(root)
    root.mainloop()


if __name__ == "__main__":
    main()
