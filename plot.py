"""A time-scaled Tk chart with inspection of individual sensor readings."""

import bisect
import math
import tkinter as tk
from datetime import datetime

BG = "#111213"
PANEL = "#1a1b1d"
EDGE = "#303134"
GOLD = "#e9c579"
TEXT = "#eeede9"
MUTED = "#aaa9a5"
DIM = "#898987"
FONT = "Segoe UI"


class TimePlot(tk.Canvas):
    def __init__(self, parent):
        super().__init__(parent, bg=PANEL, highlightthickness=0, width=200, height=85)
        self.points = []
        self.start, self.end = 0, 1
        self.battery = False
        self.locations = []
        self.bind("<Configure>", lambda _: self.draw())
        self.bind("<Motion>", self.hover)
        self.bind("<Leave>", lambda _: self.delete("tip"))

    def show(self, points, start, end, battery=False):
        self.points, self.start, self.end, self.battery = points, start, end, battery
        self.draw()

    def draw(self):
        self.delete("all")
        self.locations = []
        width, height = self.winfo_width(), self.winfo_height()
        if width < 100 or height < 45:
            return
        left, right, top, bottom = 9, width-44, 9, height-21
        values = [p[1] for p in self.points]
        if self.battery:
            low, high = 0, 100
        elif values:
            low = max(0, math.floor((min(values)-4)/10)*10)
            high = max(low+20, math.ceil((max(values)+4)/10)*10)
        else:
            low, high = 0, 100
        divisions = 2 if height >= 80 else 1
        for j in range(divisions+1):
            y = top + (bottom-top)*j/divisions
            self.create_line(left,y,right,y,fill=EDGE,dash=(2,4))
            if values or self.battery:
                label = f"{(high-(high-low)*j/divisions):g}" + ("%" if self.battery else "")
                self.create_text(right+8,y,anchor="w",text=label,fill=DIM,font=(FONT,9))
        span = self.end-self.start
        for j in range(5):
            x = left+(right-left)*j/4
            stamp = datetime.fromtimestamp(self.start + span*j/4)
            self.create_text(x,bottom+14,anchor=("w" if j==0 else "e" if j==4 else "center"),
                             text=stamp.strftime("%b %d" if span>86400 else "%H:%M"),fill=DIM,font=(FONT,9))
        if not values:
            self.create_text((left+right)/2,(top+bottom)/2,text="No readings in this time range",
                             fill=MUTED,font=(FONT,11))
            return
        previous = None
        for stamp, value, interval in self.points:
            x = left+(right-left)*(stamp-self.start)/span
            y = bottom-(bottom-top)*(value-low)/(high-low)
            joined=False
            if previous:
                px,py,pt,period = previous
                # Leave a break after missed cycles. Compressed views cover wider buckets.
                threshold=22 if interval==1 else 1.5*interval+5
                if period == interval and stamp-pt <= max(threshold, span/600):
                    self.create_line(px,py,x,y,fill=GOLD,width=2)
                    joined=True
            if len(values)<170 or not joined:
                self.create_oval(x-2,y-2,x+2,y+2,fill=GOLD,outline="")
            self.locations.append((x,y,stamp,value))
            previous=(x,y,stamp,interval)
        x,y,_,_ = self.locations[-1]
        self.create_oval(x-4,y-4,x+4,y+4,fill=GOLD,outline=PANEL,width=1)

    def hover(self, event):
        self.delete("tip")
        if not self.locations:
            return
        i=bisect.bisect_left(self.locations,(event.x,))
        x,y,stamp,value=min(self.locations[max(0,i-1):i+1],key=lambda p: abs(p[0]-event.x))
        if abs(x-event.x)>23:
            return
        self.create_line(x,10,x,self.winfo_height()-27,fill=DIM,dash=(2,3),tags="tip")
        tx=min(max(x,74),self.winfo_width()-77)
        ty=25 if y>50 else max(25,self.winfo_height()-48)
        self.create_rectangle(tx-72,ty-12,tx+72,ty+12,fill=BG,outline=EDGE,tags="tip")
        label=datetime.fromtimestamp(stamp).strftime("%H:%M:%S") + f"   {value} " + ("%" if self.battery else "BPM")
        self.create_text(tx,ty,text=label,fill=TEXT,font=(FONT,9),tags="tip")
