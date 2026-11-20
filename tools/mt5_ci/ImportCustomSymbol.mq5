//+------------------------------------------------------------------+
//|                                         ImportCustomSymbol.mq5   |
//|  CI helper: build a custom symbol from an MT5 "export bars" CSV. |
//|                                                                  |
//|  Runs headless from a terminal [StartUp] config, imports the CSV |
//|  found in MQL5\Files, replaces the symbol's rates and writes a   |
//|  status file the workflow asserts on. No broker connection is    |
//|  needed: a custom symbol carries its own history.                |
//+------------------------------------------------------------------+
#property copyright "trading-platform CI"
#property version   "1.00"
#property strict

// Parameters are read from a key=value file in MQL5\Files so the
// workflow never has to quote arguments through a .ini ScriptParameters
// line. The default name matches what
// .github/workflows/mt5-backtest.yml writes.
input string InpParamsFile = "mt5_ci_import.txt"; // Params file in MQL5\Files
input string InpStatusFile = "mt5_ci_import_status.txt"; // Status file in MQL5\Files

//+------------------------------------------------------------------+
string Trim(string value)
{
   StringTrimLeft(value);
   StringTrimRight(value);
   return value;
}

//+------------------------------------------------------------------+
bool ReadParams(const string file_name, string &keys[], string &values[])
{
   int handle = FileOpen(file_name, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ);
   if(handle == INVALID_HANDLE)
   {
      PrintFormat("ImportCustomSymbol: cannot open params %s (error %d)", file_name, GetLastError());
      return false;
   }

   while(!FileIsEnding(handle))
   {
      string line = Trim(FileReadString(handle));
      if(line == "")
         continue;
      ushort first = StringGetCharacter(line, 0);
      if(first == ';' || first == '#')
         continue;
      int sep = StringFind(line, "=");
      if(sep <= 0)
         continue;
      string key = Trim(StringSubstr(line, 0, sep));
      string value = Trim(StringSubstr(line, sep + 1));
      if(key == "")
         continue;
      int size = ArraySize(keys);
      ArrayResize(keys, size + 1);
      ArrayResize(values, size + 1);
      keys[size] = key;
      values[size] = value;
   }
   FileClose(handle);
   return true;
}

//+------------------------------------------------------------------+
string Param(const string &keys[], const string &values[], const string key, const string fallback)
{
   for(int i = 0; i < ArraySize(keys); i++)
      if(keys[i] == key)
         return values[i];
   return fallback;
}

//+------------------------------------------------------------------+
bool WriteStatus(const string file_name, const string text)
{
   int handle = FileOpen(file_name, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE)
   {
      PrintFormat("ImportCustomSymbol: cannot write status %s (error %d)", file_name, GetLastError());
      return false;
   }
   FileWriteString(handle, text);
   FileClose(handle);
   return true;
}

//+------------------------------------------------------------------+
//| Read the tab separated MT5 "export bars" CSV into MqlRates.      |
//| Header: <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL>      |
//|         <VOL> <SPREAD>  (spread in points)                       |
//+------------------------------------------------------------------+
bool ReadRates(const string file_name, MqlRates &rates[], string &error)
{
   int handle = FileOpen(file_name, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ);
   if(handle == INVALID_HANDLE)
   {
      error = StringFormat("cannot open %s (error %d)", file_name, GetLastError());
      return false;
   }

   ArrayResize(rates, 0);
   int count = 0;
   int line_no = 0;
   const ushort tab = StringGetCharacter("\t", 0);

   while(!FileIsEnding(handle))
   {
      string line = Trim(FileReadString(handle));
      line_no++;
      if(line == "")
         continue;
      if(StringGetCharacter(line, 0) == '<')   // header row
         continue;

      string parts[];
      int columns = StringSplit(line, tab, parts);
      if(columns < 9)
      {
         error = StringFormat("line %d: expected 9 tab separated columns, got %d", line_no, columns);
         FileClose(handle);
         return false;
      }

      datetime bar_time = StringToTime(parts[0] + " " + parts[1]);
      if(bar_time == 0)
      {
         error = StringFormat("line %d: unparseable timestamp %s %s", line_no, parts[0], parts[1]);
         FileClose(handle);
         return false;
      }

      if(count >= ArraySize(rates))
         ArrayResize(rates, count == 0 ? 65536 : count * 2);

      rates[count].time        = bar_time;
      rates[count].open        = StringToDouble(parts[2]);
      rates[count].high        = StringToDouble(parts[3]);
      rates[count].low         = StringToDouble(parts[4]);
      rates[count].close       = StringToDouble(parts[5]);
      rates[count].tick_volume = (long)StringToInteger(parts[6]);
      rates[count].real_volume = (long)StringToInteger(parts[7]);
      rates[count].spread      = (int)StringToInteger(parts[8]);
      count++;
   }

   FileClose(handle);
   ArrayResize(rates, count);
   return true;
}

//+------------------------------------------------------------------+
void SetProperties(const string symbol, const string &keys[], const string &values[])
{
   int digits        = (int)StringToInteger(Param(keys, values, "digits", "5"));
   double point      = StringToDouble(Param(keys, values, "point", "0.00001"));
   double contract   = StringToDouble(Param(keys, values, "contract_size", "100000"));
   double tick_value = StringToDouble(Param(keys, values, "tick_value", "0"));
   if(tick_value <= 0.0)
      tick_value = point * contract;
   double volume_min  = StringToDouble(Param(keys, values, "volume_min", "0.01"));
   double volume_step = StringToDouble(Param(keys, values, "volume_step", "0.01"));
   double volume_max  = StringToDouble(Param(keys, values, "volume_max", "100"));
   string currency_base   = Param(keys, values, "currency_base", "USD");
   string currency_profit = Param(keys, values, "currency_profit", "USD");
   string currency_margin = Param(keys, values, "currency_margin", "USD");
   string calc_mode_name  = Param(keys, values, "calc_mode", "forex");

   long calc_mode = SYMBOL_CALC_MODE_FOREX;
   if(calc_mode_name == "cfd")
      calc_mode = SYMBOL_CALC_MODE_CFD;
   else if(calc_mode_name == "cfdindex")
      calc_mode = SYMBOL_CALC_MODE_CFDINDEX;

   CustomSymbolSetInteger(symbol, SYMBOL_DIGITS, digits);
   CustomSymbolSetDouble(symbol, SYMBOL_POINT, point);
   CustomSymbolSetInteger(symbol, SYMBOL_TRADE_CALC_MODE, (int)calc_mode);
   CustomSymbolSetDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE, contract);
   CustomSymbolSetDouble(symbol, SYMBOL_TRADE_TICK_SIZE, point);
   CustomSymbolSetDouble(symbol, SYMBOL_TRADE_TICK_VALUE, tick_value);
   CustomSymbolSetDouble(symbol, SYMBOL_TRADE_TICK_VALUE_PROFIT, tick_value);
   CustomSymbolSetDouble(symbol, SYMBOL_TRADE_TICK_VALUE_LOSS, tick_value);
   CustomSymbolSetDouble(symbol, SYMBOL_VOLUME_MIN, volume_min);
   CustomSymbolSetDouble(symbol, SYMBOL_VOLUME_STEP, volume_step);
   CustomSymbolSetDouble(symbol, SYMBOL_VOLUME_MAX, volume_max);
   CustomSymbolSetString(symbol, SYMBOL_CURRENCY_BASE, currency_base);
   CustomSymbolSetString(symbol, SYMBOL_CURRENCY_PROFIT, currency_profit);
   CustomSymbolSetString(symbol, SYMBOL_CURRENCY_MARGIN, currency_margin);
   CustomSymbolSetInteger(symbol, SYMBOL_TRADE_MODE, SYMBOL_TRADE_MODE_FULL);
   CustomSymbolSetInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL, 0);
   CustomSymbolSetInteger(symbol, SYMBOL_TRADE_FREEZE_LEVEL, 0);
   CustomSymbolSetInteger(symbol, SYMBOL_TRADE_EXEMODE, SYMBOL_TRADE_EXECUTION_INSTANT);
   CustomSymbolSetInteger(symbol, SYMBOL_CHART_MODE, SYMBOL_CHART_MODE_BID);
   CustomSymbolSetInteger(symbol, SYMBOL_SPREAD_FLOAT, 0);
}

//+------------------------------------------------------------------+
void OnStart()
{
   string keys[];
   string values[];

   if(!ReadParams(InpParamsFile, keys, values))
   {
      WriteStatus(InpStatusFile, "FAIL cannot read params file " + InpParamsFile);
      TerminalClose(1);
      return;
   }

   string symbol = Param(keys, values, "symbol", "");
   string csv    = Param(keys, values, "csv", "");
   if(symbol == "" || csv == "")
   {
      WriteStatus(InpStatusFile, "FAIL params must define symbol and csv");
      TerminalClose(1);
      return;
   }

   MqlRates rates[];
   string error = "";
   if(!ReadRates(csv, rates, error))
   {
      WriteStatus(InpStatusFile, "FAIL read " + csv + ": " + error);
      TerminalClose(1);
      return;
   }
   if(ArraySize(rates) == 0)
   {
      WriteStatus(InpStatusFile, "FAIL no bars parsed from " + csv);
      TerminalClose(1);
      return;
   }

   if(!CustomSymbolCreate(symbol, "mt5_ci", ""))
   {
      // Already present (e.g. from an earlier run in the same portable
      // directory): rebuild it from scratch so properties never leak
      // between runs. A symbol still selected in Market Watch cannot be
      // deleted, so deselect it first.
      SymbolSelect(symbol, false);
      if(!CustomSymbolDelete(symbol) || !CustomSymbolCreate(symbol, "mt5_ci", ""))
      {
         WriteStatus(InpStatusFile, StringFormat("FAIL CustomSymbolCreate(%s) error %d", symbol, GetLastError()));
         TerminalClose(1);
         return;
      }
   }

   SetProperties(symbol, keys, values);
   SymbolSelect(symbol, true);

   CustomRatesDelete(symbol, 0, LONG_MAX);
   int replaced = CustomRatesReplace(symbol, rates[0].time, rates[ArraySize(rates) - 1].time, rates);
   if(replaced <= 0)
   {
      WriteStatus(InpStatusFile, StringFormat("FAIL CustomRatesReplace(%s) returned %d error %d",
                                              symbol, replaced, GetLastError()));
      TerminalClose(1);
      return;
   }

   int bars = Bars(symbol, PERIOD_M1);
   if(bars <= 0)
   {
      WriteStatus(InpStatusFile, StringFormat("FAIL %s has no M1 bars after import", symbol));
      TerminalClose(1);
      return;
   }

   string status = StringFormat("OK %s bars=%d first=%s last=%s replaced=%d",
                                symbol, bars,
                                TimeToString(rates[0].time, TIME_DATE | TIME_MINUTES),
                                TimeToString(rates[ArraySize(rates) - 1].time, TIME_DATE | TIME_MINUTES),
                                replaced);
   PrintFormat("ImportCustomSymbol: %s", status);
   WriteStatus(InpStatusFile, status);
   // Give the terminal a moment to flush the custom symbol history to
   // bases\Custom before it exits; the tester runs in a later process.
   Sleep(2000);
   TerminalClose(0);
}
//+------------------------------------------------------------------+
