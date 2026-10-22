"""Keep inspector columns and pointer targets aligned in a smaller native canvas."""


class ScaledCanvas:
    def __init__(self,native,minimum_width=1100):
        self.native=native
        self.minimum_width=minimum_width
        self.scale=1.0
        self._size=(0,0)
        self.resize()

    def __getattr__(self,name):return getattr(self.native,name)

    def __setattr__(self,name,value):
        if name in ('native','minimum_width','scale','_size'):object.__setattr__(self,name,value)
        else:setattr(self.native,name,value)

    def resize(self):
        size=(int(self.native.width),int(self.native.height))
        if min(size)<=0 or size==self._size:return False
        self._size=size
        self.scale=min(1.0,size[0]/self.minimum_width)
        return True

    @property
    def width(self):return round(self._size[0]/self.scale)

    @property
    def height(self):return round(self._size[1]/self.scale)

    def _point(self,p):return (p[0]*self.scale,p[1]*self.scale)

    def _click(self,p):return None if p is None else (p[0]/self.scale,p[1]/self.scale)

    def take_click(self):return self._click(self.native.take_click())

    def take_right_click(self):return self._click(self.native.take_right_click())

    def mouse_pos(self):
        x,y,inside=self.native.mouse_pos()
        return x/self.scale,y/self.scale,inside

    def text(self,pos,color,text,*,size=13,**kwargs):
        return self.native.text(self._point(pos),color,text,size=size*self.scale,**kwargs)

    def line(self,a,b,color,thickness=1):
        return self.native.line(self._point(a),self._point(b),color,thickness*self.scale)

    def rect(self,a,b,color,rounding=0,thickness=1):
        return self.native.rect(self._point(a),self._point(b),color,rounding*self.scale,thickness*self.scale)

    def rect_filled(self,a,b,color,rounding=0):
        return self.native.rect_filled(self._point(a),self._point(b),color,rounding*self.scale)

    def triangle_filled(self,a,b,c,color):
        return self.native.triangle_filled(self._point(a),self._point(b),self._point(c),color)
