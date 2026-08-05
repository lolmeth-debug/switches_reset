import win32ui, win32con, win32print
from PIL import Image, ImageWin

class CPrinter:
   curprintername = ''
   curprinter = None

   def __init__(self):
       pass

   def printfile(self, fn, printer_name=None):  # Добавляем параметр printer_name
        # Если имя принтера не указано, используем текущий
        if printer_name is None:
            printer_name = self.curprintername
            if not printer_name:  # Если текущий не установлен, пытаемся найти ZD410
                printer_name = self.find('ZD410')

        try:
            img = Image.open(fn, 'r')
        except:
            print(f"Error open file '%s'" % (fn))
            return

        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer_name)  # Используем переданное имя принтера

        horzres = hdc.GetDeviceCaps(win32con.HORZRES)
        vertres = hdc.GetDeviceCaps(win32con.VERTRES)

        landscape = horzres > vertres
        if landscape:
            if img.size[1] > img.size[0]:
                print('Landscape mode, tall image, rotate bitmap.')
                img = img.rotate(90, expand=True)
        else:
            if img.size[1] < img.size[0]:
                print('Portrait mode, wide image, rotate bitmap.')
                img = img.rotate(90, expand=True)

        img_width = img.size[0]
        img_height = img.size[1]

        if landscape:
            #we want image width to match page width
            ratio = vertres / horzres
            max_width = img_width
            max_height = (int)(img_width * ratio)
        else:
            #we want image height to match page height
            ratio = horzres / vertres
            max_height = img_height
            max_width = (int)(max_height * ratio)

        #map image size to page size
        hdc.SetMapMode(win32con.MM_ISOTROPIC)
        hdc.SetViewportExt((horzres, vertres));
        hdc.SetWindowExt((max_width, max_height))

        #offset image so it is centered horizontally
        offset_x = (int)((max_width - img_width)/2)
        offset_y = (int)((max_height - img_height)/2)
        hdc.SetWindowOrg((-offset_x, -offset_y))

        print( 'Debug info:' )
        print( 'Landscape: %d' % landscape )
        print( 'horzres: %d' % horzres )
        print( 'vertres: %d' % vertres )
        print( 'img_width: %d' % img_width )
        print( 'img_height: %d' % img_height )
        print( 'max_width: %d' % max_width )
        print( 'max_height: %d' % max_height )
        print( 'offset_x: %d' % offset_x )
        print( 'offset_y: %d' % offset_y )

        hdc.StartDoc('Result')
        hdc.StartPage()

        dib = ImageWin.Dib(img)
        dib.draw(hdc.GetHandleOutput(), (0, 0, img_width, img_height))

        hdc.EndPage()
        hdc.EndDoc()
        hdc.DeleteDC()

   def get_available_printers(self):
       """Возвращает список доступных принтеров"""
       printers_info = win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL |
                                             win32print.PRINTER_ENUM_CONNECTIONS)
       return [printer[2] for printer in printers_info]  # Возвращаем только имена

   def printers_list( self ):
       printers = win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL)
       printercount = 0
       printername = ''
       for i,x in enumerate( printers ):
           print( i, "-", x[2])

   def find( self, name ):
       printers = win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL)
       printercount = 0
       self.curprintername = ''
       for i,x in enumerate( printers ):

           if name in x[1]:
               self.curprinter = x
               self.curprintername = x[2]
               print("selected printer: ", self.curprinter[1])
               break
       return self.curprintername

   def getCurrent(self):
      return self.curprintername
