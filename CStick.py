'''
Отвечает за формирование изображение с QR-кодом (мак-адрес), названием устройства, прошивки
'''


#!/bin/python3
import random
import uuid

from PIL import Image, ImageDraw, ImageFont
import qrcode
import datetime, locale

class CStick():
   bkgw = 336
   bkgh = 200
   objects = []
   qr = {}
   resimg = None

   def __init__(self, width=336, height=200, dpi=0 ):
      if dpi==0:
         self.bkgw = width
         self.bkgh = height
      else:
         self.bkgw = int((width*dpi)/25.4)
         self.bkgh = int((height*dpi)/25.4)

      self.qr['text'] = "text"
      self.qr['size'] = 6
      self.qr['posx'] = 'center'
      self.qr['posy'] = 'center'
      self.objects = []

      locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')
      locale.setlocale(locale.LC_ALL, 'Russian')

   def createtextimg( self, text, textsize, rotate=90, bold=2,fontname='arial.ttf' ):
      fname = f"./%s" % (fontname)
      font = ImageFont.truetype( fname, textsize )
      x,y,w,h = font.getbbox( text )
      img = Image.new('RGBA', (w,h), (255,255, 255,255))
      draw = ImageDraw.Draw(img)
      draw.text((0, 0), text=text, font=font, fill=(0,0,0))
      img = img.rotate(rotate, expand=1)
      w,h = img.size
#      print( "w", w, "h", h )
      return img

   def createQR( self, text, fn="qr_code.png", box_size=6 ):
      # Create a QR code object with a larger size and higher error correction
      qr = qrcode.QRCode(version=3, box_size=box_size, border=1, error_correction=qrcode.constants.ERROR_CORRECT_H)
      # Define the data to be encoded in the QR code
      # Add the data to the QR code object
      qr.add_data(text)
      # Make the QR code
      qr.make(fit=True)
      # Create an image from the QR code with a black fill color and white background
      img = qr.make_image(fill_color="black", back_color="white")
      # Save the QR code image
      if fn!="":
         img.save(fn)
      return img

#   def addText( self, text, posx, posy, size, rotate=0, fontname='arial.ttf' ):
   def addText( self, text, posx, posy, size, rotate=0, fontname='Bahnschrift.ttf' ):
      obj = {'text':text,'posx':posx,'posy':posy,'size':size,'rotate':rotate,'fontname':fontname}
      self.objects.append( obj )

   def addQR( self, text, posx, posy, size ):
      self.qr['text'] = text
      self.qr['size'] = size
      self.qr['posx'] = posx
      self.qr['posy'] = posy

   def check_int(self,s):
      if s[0] in ('-', '+'):
         return s[1:].isdigit()
      return s.isdigit()

   def parsePos( self, pos, imgsize, bkgsize ):
      posres = 0
      if type(pos)==int:
          posres=pos
      elif type(pos)==str:
          if 'center' == pos:
             posres = int( ( bkgsize - imgsize )/2 )
          elif 'center' in pos:
             ps = pos.replace('center','')
             if self.check_int( ps ):
                posres = int( ( bkgsize - imgsize )/2 )+int(ps)
          elif 'right' == pos or 'bottom' == pos:
             posres = int( bkgsize - imgsize )
          elif 'right' in pos or 'bottom' in pos:
             ps = pos.replace('right','').replace('bottom','')
             if self.check_int( ps ):
                posres = int( bkgsize - imgsize )+int(ps)

      return posres

   def create( self, repair=False, spisanie=False ):

      bkg_img = Image.new('RGBA', (self.bkgw,self.bkgh), (255,255,255,255))
#      draw = ImageDraw.Draw(bkg_img)

      if repair:
         imgqr = Image.open( "remont.png" )
      elif spisanie:
         imgqr = Image.open( "spisanie.png" )
      else:
         imgqr = self.createQR( text=self.qr['text'], box_size=self.qr['size'], fn="qr.png" )
         imgqr = Image.open( "qr.png" )
      qrw, qrh = imgqr.size
      print( "new img qr", imgqr.size )

      posx = self.parsePos( self.qr['posx'], qrw, self.bkgw )
      posy = self.parsePos( self.qr['posy'], qrh, self.bkgh )
      bkg_img.paste( imgqr, (posx,posy))

      for obj in self.objects:
         img = self.createtextimg( obj['text'], obj['size'], obj['rotate'], fontname=obj['fontname'] )
         qrw,qrh = img.size
         print( "new img for text '%s' w,h: %s,%s" % (obj['text'], qrw, qrh) )
         posx = self.parsePos( obj['posx'], qrw, self.bkgw )
         posy = self.parsePos( obj['posy'], qrh, self.bkgh )
         bkg_img.paste( img, (posx, posy))
         del img

      self.resimg = bkg_img
      return self.resimg

   def save(self, fnout='stick.png'):
      self.resimg.save( fnout )
      print( f"image saved to {fnout}" )
      return

   def createStick( self, mac, model, hwver, fnout='back.png' ):
      imgmodel = self.createtextimg( model, 25 )
      imgmodelw, imgmodelh = imgmodel.size

      imghw = self.createtextimg( hwver, 25 )
      imghww, imghwh = imghw.size

      imgmac = self.createtextimg( mac, 23 )
      imgmacw, imgmach = imgmac.size

      timenow = datetime.datetime.today().strftime(u'%d %B %Y')
      imgtime = self.createtextimg( timenow, 20 )
      imgtimew, imgtimeh = imgtime.size

      imgqr = self.createQR( mac )

      bkg_img = Image.new('RGBA', (self.bkgw,self.bkgh), (255,255,255,255))
      draw = ImageDraw.Draw(bkg_img)

      posy = int((self.bkgh-imgmodelh)/2)
      print( "imgmodel h", imgmodelh, "posy", posy )
      bkg_img.paste( imgmodel, (20, posy ))

      posy = int((self.bkgh-imgmach)/2)
      print( "imgmac h", imgmach,"posy", posy )
      bkg_img.paste( imgmac, (85, posy ))

      posy = int((self.bkgh-imghwh)/2)
      print( "imgmac h", imghwh,"posy", posy )
      bkg_img.paste( imghw, (55, posy ))

      posy = int((self.bkgh-imgtimeh)/2)
      print( "imgtime h", imgmach,"posy", posy )
      bkg_img.paste( imgtime, (300, posy ))

  #   qrimg = Image.open('qr_code.png')
      qrw, qrh = imgqr.size

      posx = int( 30+(336 - qrw )/2 )
      posy = int( (200 - qrh )/2 )
      print( "qrw", qrw, "qrh", qrh )
      print( "imgqr posx", posx, "posy", posy )
      bkg_img.paste( imgqr, (posx,posy))
      bkg_img.save( fnout )
      return bkg_img

if __name__=='__main__':
   mac = "11:22:33:44:55:66"
   model = "DES-3526"
   hwver = "hw: A4G"

#   CStick(336,200).createStick( mac, model, hwver, 'back1.png' )
#   stick = CStick( 336,200 )

   """
   stick = CStick( 56,35, dpi=203 ) #size in mm
   stick.addQR( mac, 'center+30','center', 6 )
   stick.addText( mac, posx='center', posy=20, size=25, rotate=90 )
   stick.addText( model, posx=50, posy='center', size=40, rotate=90 )
   stick.addText( hwver, posx=350, posy='center', size=40, rotate=270 )

   timenow = datetime.datetime.today().strftime(u'%d %B %Y')
   stick.addText( timenow, posx='center', posy=240, size=30, rotate=0, fontname='Bahnschrift.ttf' )

   stick.create()
   stick.save('stick.png')
   del stick
   """

   mac = "DD:DD:DD:DD:DD:DD"
   model = "DES-3526"
   hwver = "hw: A4G"

#  stick1 = CStick( 42,25, dpi=203 ) #size in mm
   stick1 = CStick( 336,200) #size in px
   stick1.addQR( mac, 'center+30','center', 6 )
   stick1.addText( model, posx=20, posy='center', size=25, rotate=90 )
   stick1.addText( hwver, posx=55, posy='center', size=25, rotate=90 )
   stick1.addText( mac, posx=85, posy='center', size=20, rotate=90 )

   timenow = datetime.datetime.today().strftime(f'%d %B %Y')
   stick1.addText( timenow, posx=300, posy='center', size=20, rotate=90 )

   stick1.create()
   stick1.save('stick_test.png')


   """
   dpi = 203
   imgw_px = 336
   imgh_px = 200
   print( "img  width {}px -> {:3.2f}mm (dpi: {} ppi)".format(imgw_px, imgw_px*25.4/dpi, dpi ) )
   print( "img height {}px -> {:3.2f}mm (dpi: {} ppi)".format(imgh_px, imgh_px*25.4/dpi, dpi ))
   """
