//+------------------------------------------------------------------+
//|                                   IntradayVolatilityBreakout.mq5 |
//|                    Copyright 2025, RobustifyTrading               |
//|                        All rights reserved                        |
//+------------------------------------------------------------------+
#property copyright "RobustifyTrading"
#property link      "https://discord.com/channels/1230977283888648315/1506277517147897998/1506277517147897998"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

//--- Enumerations
enum ENUM_REGIME_MODE
{
   REGIME_LOW_VOLATILITY  = 0,  // Low Volatility Regime
   REGIME_HIGH_VOLATILITY = 1   // High Volatility Regime
};

enum ENUM_TRADE_DIRECTION
{
   TRADE_BOTH       = 0,  // Both Directions
   TRADE_LONG_ONLY  = 1,  // Long Only
   TRADE_SHORT_ONLY = 2   // Short Only
};

//--- Input Parameters
input group "Breakout Parameters"
input int                ATR_Period              = 5;        // ATR Period (Daily)
input double             ATR_Multiplier          = 0.25;     // ATR Multiplier for Breakout Levels
input double             Stop_ATR_Multiplier     = 0.25;     // ATR Multiplier for Stop Loss
input bool               Use_Candle_Close        = true;     // Require Candle Close Confirmation
input ENUM_TIMEFRAMES    Confirmation_Timeframe   = PERIOD_M15;// Confirmation Timeframe

input group "Regime Filter"
input int                ATR_Short_Period        = 1;        // Short-Term ATR Period (Daily)
input int                ATR_Long_Period         = 5;       // Long-Term ATR Period (Daily)
input double             ATR_Regime_Factor       = 1.0;      // Regime Factor
input ENUM_REGIME_MODE   Regime_Filter_Mode      = REGIME_LOW_VOLATILITY; // Regime Filter Mode

input group "Trade Direction"
input ENUM_TRADE_DIRECTION Trade_Direction        = TRADE_BOTH; // Allowed Trade Direction
input bool               Reverse_Mode            = false;    // Reverse Mode (Fade Breakout)

input group "Position Management"
input double             Fixed_Risk_Amount       = 500.0;    // Fixed Risk Amount (Account Currency)

input group "Trading Hours"
input string             Entry_Time              = "16:30";  // Entry Window Start (HH:MM)
input string             Max_Entry_Time          = "18:05";  // Latest Entry Time (HH:MM)
input string             Exit_Time               = "22:55";  // Close All Positions Time (HH:MM)

input group "Risk Management"
input int                Magic_Number            = 123456;   // Magic Number

//--- Global Variables
CTrade   g_trade;
double   g_atr_value;
double   g_entry_open_price;
double   g_upper_level;
double   g_lower_level;
double   g_stop_distance;
bool     g_setup_ready;
bool     g_levels_calculated;
bool     g_trade_taken;
datetime g_last_bar_time;
datetime g_last_confirm_bar;
int      g_atr_handle;
int      g_atr_long_handle;
int      g_atr_short_handle;
int      g_entry_hour,    g_entry_min;
int      g_max_entry_hour, g_max_entry_min;
int      g_exit_hour,     g_exit_min;

//+------------------------------------------------------------------+
//| Initialization                                                    |
//+------------------------------------------------------------------+
int OnInit()
{
   g_trade.SetExpertMagicNumber(Magic_Number);
   g_trade.SetDeviationInPoints(10);
   g_trade.SetTypeFilling(ORDER_FILLING_FOK);

   g_atr_handle = iATR(_Symbol, PERIOD_D1, ATR_Period);
   if(g_atr_handle == INVALID_HANDLE)
   {
      Print("Error: Failed to create ATR indicator handle");
      return INIT_FAILED;
   }

   g_atr_long_handle = iATR(_Symbol, PERIOD_D1, ATR_Long_Period);
   if(g_atr_long_handle == INVALID_HANDLE)
   {
      Print("Error: Failed to create long-term ATR indicator handle");
      return INIT_FAILED;
   }

   g_atr_short_handle = iATR(_Symbol, PERIOD_D1, ATR_Short_Period);
   if(g_atr_short_handle == INVALID_HANDLE)
   {
      Print("Error: Failed to create short-term ATR indicator handle");
      return INIT_FAILED;
   }

   if(!ParseTime(Entry_Time, g_entry_hour, g_entry_min))
   {
      Print("Error: Invalid Entry_Time format. Use HH:MM");
      return INIT_FAILED;
   }

   if(!ParseTime(Max_Entry_Time, g_max_entry_hour, g_max_entry_min))
   {
      Print("Error: Invalid Max_Entry_Time format. Use HH:MM");
      return INIT_FAILED;
   }

   if(!ParseTime(Exit_Time, g_exit_hour, g_exit_min))
   {
      Print("Error: Invalid Exit_Time format. Use HH:MM");
      return INIT_FAILED;
   }

   int entry_mins    = g_entry_hour * 60 + g_entry_min;
   int max_entry_mins = g_max_entry_hour * 60 + g_max_entry_min;
   int exit_mins     = g_exit_hour * 60 + g_exit_min;

   if(entry_mins >= max_entry_mins)
   {
      Print("Error: Entry_Time must be earlier than Max_Entry_Time");
      return INIT_FAILED;
   }

   if(max_entry_mins >= exit_mins)
   {
      Print("Error: Max_Entry_Time must be earlier than Exit_Time");
      return INIT_FAILED;
   }

   ResetDailyState();

   Print("Intraday Volatility Breakout EA initialized");
   Print("ATR Period: ", ATR_Period, " | Multiplier: ", ATR_Multiplier, " | Stop Multiplier: ", Stop_ATR_Multiplier);
   Print("Regime: Short(", ATR_Short_Period, ") vs Long(", ATR_Long_Period, ") | Factor: ", ATR_Regime_Factor);
   Print("Direction: ", DirectionToString(), " | Reverse: ", Reverse_Mode ? "On" : "Off");
   Print("Hours: ", Entry_Time, " - ", Max_Entry_Time, " | Exit: ", Exit_Time);
   Print("Confirmation: ", Use_Candle_Close ? "Candle Close (" + EnumToString(Confirmation_Timeframe) + ")" : "Immediate Touch");
   Print("Risk: ", Fixed_Risk_Amount, " ", AccountInfoString(ACCOUNT_CURRENCY));

   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Deinitialization                                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(g_atr_handle != INVALID_HANDLE)
      IndicatorRelease(g_atr_handle);
   if(g_atr_long_handle != INVALID_HANDLE)
      IndicatorRelease(g_atr_long_handle);
   if(g_atr_short_handle != INVALID_HANDLE)
      IndicatorRelease(g_atr_short_handle);
}

//+------------------------------------------------------------------+
//| Main Tick Handler                                                 |
//+------------------------------------------------------------------+
void OnTick()
{
   if(PositionSelect(_Symbol))
   {
      if(ShouldClosePosition())
      {
         CloseAllPositions();
         return;
      }
   }

   if(Use_Candle_Close)
   {
      datetime bar_time = iTime(_Symbol, Confirmation_Timeframe, 0);
      if(bar_time == g_last_confirm_bar)
         return;
      g_last_confirm_bar = bar_time;
   }

   datetime current_bar = iTime(_Symbol, PERIOD_M1, 0);
   if(current_bar != g_last_bar_time)
      g_last_bar_time = current_bar;

   if(IsNewDay())
   {
      ResetDailyState();
      Print("New trading day detected");
   }

   if(!g_levels_calculated && IsAtOrPastTime(g_entry_hour, g_entry_min))
   {
      if(CalculateLevels())
      {
         g_levels_calculated = true;
         g_setup_ready      = true;
         PrintLevels();
      }
   }

   if(!g_setup_ready || g_trade_taken || !g_levels_calculated)
      return;

   if(!IsInEntryWindow())
      return;

   if(PositionSelect(_Symbol))
      return;

   if(!PassesRegimeFilter())
      return;

   EvaluateSignals();
}

//+------------------------------------------------------------------+
//| Core Signal Evaluation                                            |
//+------------------------------------------------------------------+
void EvaluateSignals()
{
   double price = GetSignalPrice();
   if(price <= 0)
      return;

   bool upper_hit = (price >= g_upper_level);
   bool lower_hit = (price <= g_lower_level);

   if(Reverse_Mode)
      EvaluateReverseSignals(upper_hit, lower_hit);
   else
      EvaluateBreakoutSignals(upper_hit, lower_hit);
}

//+------------------------------------------------------------------+
//| Breakout (Follow) Signals                                         |
//+------------------------------------------------------------------+
void EvaluateBreakoutSignals(bool upper_hit, bool lower_hit)
{
   if(upper_hit && (Trade_Direction == TRADE_BOTH || Trade_Direction == TRADE_LONG_ONLY))
      OpenLong("Breakout Long");

   if(lower_hit && (Trade_Direction == TRADE_BOTH || Trade_Direction == TRADE_SHORT_ONLY))
      OpenShort("Breakout Short");
}

//+------------------------------------------------------------------+
//| Reverse (Fade) Signals                                            |
//+------------------------------------------------------------------+
void EvaluateReverseSignals(bool upper_hit, bool lower_hit)
{
   if(upper_hit && (Trade_Direction == TRADE_BOTH || Trade_Direction == TRADE_SHORT_ONLY))
      OpenShort("Fade Short");

   if(lower_hit && (Trade_Direction == TRADE_BOTH || Trade_Direction == TRADE_LONG_ONLY))
      OpenLong("Fade Long");
}

//+------------------------------------------------------------------+
//| Get Signal Price Based on Confirmation Mode                       |
//+------------------------------------------------------------------+
double GetSignalPrice()
{
   if(Use_Candle_Close)
      return iClose(_Symbol, Confirmation_Timeframe, 1);

   return SymbolInfoDouble(_Symbol, SYMBOL_LAST);
}

//+------------------------------------------------------------------+
//| Regime Filter Check                                               |
//+------------------------------------------------------------------+
bool PassesRegimeFilter()
{
   double long_buf[], short_buf[];
   ArraySetAsSeries(long_buf, true);
   ArraySetAsSeries(short_buf, true);

   if(CopyBuffer(g_atr_long_handle, 0, 1, 1, long_buf) <= 0)
      return false;

   if(CopyBuffer(g_atr_short_handle, 0, 1, 1, short_buf) <= 0)
      return false;

   double short_atr = short_buf[0];
   double threshold = long_buf[0] * ATR_Regime_Factor;

   if(Regime_Filter_Mode == REGIME_LOW_VOLATILITY)
      return (short_atr < threshold);
   else
      return (short_atr > threshold);
}

//+------------------------------------------------------------------+
//| Calculate Breakout Levels                                         |
//+------------------------------------------------------------------+
bool CalculateLevels()
{
   double buf[];
   ArraySetAsSeries(buf, true);

   if(CopyBuffer(g_atr_handle, 0, 1, 1, buf) <= 0)
   {
      Print("Error: Failed to read ATR buffer");
      return false;
   }

   g_atr_value = buf[0];

   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   dt.hour = g_entry_hour;
   dt.min  = g_entry_min;
   dt.sec  = 0;

   int shift = iBarShift(_Symbol, PERIOD_M1, StructToTime(dt));
   if(shift < 0)
   {
      Print("Error: Could not locate bar at entry time");
      return false;
   }

   g_entry_open_price = iOpen(_Symbol, PERIOD_M1, shift);
   if(g_entry_open_price <= 0)
   {
      Print("Error: Invalid open price at entry time");
      return false;
   }

   g_upper_level  = g_entry_open_price + ATR_Multiplier * g_atr_value;
   g_lower_level  = g_entry_open_price - ATR_Multiplier * g_atr_value;
   g_stop_distance = Stop_ATR_Multiplier * g_atr_value;

   return true;
}

//+------------------------------------------------------------------+
//| Open Long Position                                                |
//+------------------------------------------------------------------+
void OpenLong(string comment)
{
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double sl  = ask - g_stop_distance;
   double lots = CalculateLotSize(g_stop_distance);

   if(g_trade.Buy(lots, _Symbol, ask, sl, 0, comment))
   {
      Print("Long opened @ ", ask, " | Lots: ", lots, " | SL: ", sl, " | ", comment);
      g_trade_taken = true;
   }
   else
      Print("Long order failed. Error: ", GetLastError());
}

//+------------------------------------------------------------------+
//| Open Short Position                                               |
//+------------------------------------------------------------------+
void OpenShort(string comment)
{
   double bid  = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double sl   = bid + g_stop_distance;
   double lots = CalculateLotSize(g_stop_distance);

   if(g_trade.Sell(lots, _Symbol, bid, sl, 0, comment))
   {
      Print("Short opened @ ", bid, " | Lots: ", lots, " | SL: ", sl, " | ", comment);
      g_trade_taken = true;
   }
   else
      Print("Short order failed. Error: ", GetLastError());
}

//+------------------------------------------------------------------+
//| Calculate Lot Size from Fixed Risk                                |
//+------------------------------------------------------------------+
double CalculateLotSize(double sl_distance)
{
   double tick_val  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double min_lot   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double max_lot   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lot_step  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   double lots = (Fixed_Risk_Amount / sl_distance) * (tick_size / tick_val);

   lots = MathFloor(lots / lot_step) * lot_step;
   lots = MathMax(lots, min_lot);
   lots = MathMin(lots, max_lot);

   return lots;
}

//+------------------------------------------------------------------+
//| Close All Positions                                               |
//+------------------------------------------------------------------+
void CloseAllPositions()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket))
      {
         if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
            PositionGetInteger(POSITION_MAGIC) == Magic_Number)
         {
            g_trade.PositionClose(ticket);
            Print("Position closed: #", ticket);
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Check If Existing Position Should Be Closed                       |
//+------------------------------------------------------------------+
bool ShouldClosePosition()
{
   if(!PositionSelect(_Symbol))
      return false;

   datetime pos_time = (datetime)PositionGetInteger(POSITION_TIME);

   MqlDateTime pos_dt, now_dt;
   TimeToStruct(pos_time, pos_dt);
   TimeToStruct(TimeCurrent(), now_dt);

   if(pos_dt.day != now_dt.day || pos_dt.mon != now_dt.mon || pos_dt.year != now_dt.year)
   {
      Print("Stale position from previous day detected");
      return true;
   }

   int now_mins  = now_dt.hour * 60 + now_dt.min;
   int exit_mins = g_exit_hour * 60 + g_exit_min;

   if(now_mins >= exit_mins)
   {
      Print("Exit time reached");
      return true;
   }

   return false;
}

//+------------------------------------------------------------------+
//| Reset Daily State                                                 |
//+------------------------------------------------------------------+
void ResetDailyState()
{
   g_setup_ready      = false;
   g_levels_calculated = false;
   g_trade_taken      = false;
   g_entry_open_price = 0;
}

//+------------------------------------------------------------------+
//| New Day Check                                                     |
//+------------------------------------------------------------------+
bool IsNewDay()
{
   static int last_day = -1;
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);

   if(dt.day != last_day)
   {
      last_day = dt.day;
      return true;
   }
   return false;
}

//+------------------------------------------------------------------+
//| Time Comparisons                                                  |
//+------------------------------------------------------------------+
bool IsAtOrPastTime(int hour, int minute)
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   return (dt.hour * 60 + dt.min >= hour * 60 + minute);
}

bool IsInEntryWindow()
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   int now = dt.hour * 60 + dt.min;
   return (now >= g_entry_hour * 60 + g_entry_min &&
           now <  g_max_entry_hour * 60 + g_max_entry_min);
}

//+------------------------------------------------------------------+
//| Parse HH:MM Time String                                          |
//+------------------------------------------------------------------+
bool ParseTime(string time_str, int &hour, int &minute)
{
   string parts[];
   if(StringSplit(time_str, ':', parts) != 2)
      return false;

   hour   = (int)StringToInteger(parts[0]);
   minute = (int)StringToInteger(parts[1]);

   return (hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59);
}

//+------------------------------------------------------------------+
//| Print Levels to Journal                                           |
//+------------------------------------------------------------------+
void PrintLevels()
{
   Print("Levels calculated | Open: ", g_entry_open_price,
         " | Upper: ", g_upper_level,
         " | Lower: ", g_lower_level,
         " | Stop: ", g_stop_distance);
   if(Reverse_Mode)
      Print("Reverse Mode active - signals will be faded");
}

//+------------------------------------------------------------------+
//| Direction to String                                               |
//+------------------------------------------------------------------+
string DirectionToString()
{
   switch(Trade_Direction)
   {
      case TRADE_BOTH:       return "Both";
      case TRADE_LONG_ONLY:  return "Long Only";
      case TRADE_SHORT_ONLY: return "Short Only";
      default:               return "Unknown";
   }
}
//+------------------------------------------------------------------+
