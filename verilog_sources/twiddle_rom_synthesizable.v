// =============================================================================
// Unified Twiddle ROM — synthesizable case-statement version
// =============================================================================
// Format per 24-bit entry: [23:16] FP8 Real | [15:8] FP8 Imag | [7:4] FP4 Real | [3:0] FP4 Imag
//
// FIX (Synthesis Runtime):
//   Replaced initial/$readmemb with a fully synthesizable registered case ROM.
//   All 512 entries are sourced directly from twiddles_1024.txt (authoritative).
//   Genus infers this as a ROM and maps it to library cells — no FFs, no
//   undriven-signal warnings, no constant-propagation storm.
//
// Conjugate symmetry: only k=0..511 are stored.
//   W_N^{N-k} = conj(W_N^k) → second half handled by flipping the imag sign bit.
//   k=512 (W=-1+0j) is hardcoded separately.
//
// Scaling: scaled_k maps runtime N to the correct 0..511 ROM index.
//   e.g. N=8, k=1 → scaled_k = 128  (= k * 1024/N/2)
// =============================================================================
`timescale 1ns/1ps

module twiddle_factor_unified #(
    parameter MAX_N      = 1024,
    parameter ADDR_WIDTH = $clog2(MAX_N) + 1
)(
    input  wire                  clk,       // registered output: 1-cycle latency
    input  wire [ADDR_WIDTH-1:0] k,         // twiddle index from AGU
    input  wire [ADDR_WIDTH-1:0] n,         // runtime FFT size
    input  wire                  PRECISION, // 0=FP4, 1=FP8
    output reg  [15:0]           twiddle_out
);

    // -------------------------------------------------------------------------
    // 1. Scale k into the 0..511 ROM index space (Fixed LINT truncation)
    // -------------------------------------------------------------------------
    reg [ADDR_WIDTH-1:0] scaled_k;

    always @(*) begin
        case (n)
            11'd1024: scaled_k = k;
            11'd512:  scaled_k = k << 1;
            11'd256:  scaled_k = k << 2;
            11'd128:  scaled_k = k << 3;
            11'd64:   scaled_k = k << 4;
            11'd32:   scaled_k = k << 5;
            11'd16:   scaled_k = k << 6;
            11'd8:    scaled_k = k << 7;
            11'd4:    scaled_k = k << 8;
            11'd2:    scaled_k = k << 9;
            default:  scaled_k = 11'd0;
        endcase
    end

    // -------------------------------------------------------------------------
    // 2. Conjugate-symmetry fold: store only k=0..511
    //    W_N^{N-k} = conj(W_N^k) → flip imag sign bit for k>511
    //    k=512 → W = -1+0j (hardcoded)
    // -------------------------------------------------------------------------
    reg        use_conj;
    reg [10:0] rom_addr;
    reg        is_mid;

    always @(*) begin
        is_mid   = 1'b0;
        use_conj = 1'b0;
        rom_addr = 11'd0;

        if (scaled_k == 11'd512) begin
            is_mid   = 1'b1;
        end else if (scaled_k > 11'd511) begin
            rom_addr = 11'd1024 - scaled_k;
            use_conj = 1'b1;
        end else begin
            rom_addr = scaled_k[9:0];
        end
    end

    // -------------------------------------------------------------------------
    // 3. ROM — full 512 entries from twiddles_1024.txt
    //    Combinational case statement; Genus infers as ROM, not flip-flops.
    // -------------------------------------------------------------------------
    reg [23:0] rom_data;

    always @(*) begin
        case (rom_addr)
            11'd0  : rom_data = 24'h380020;
            11'd1  : rom_data = 24'h388028;
            11'd2  : rom_data = 24'h388528;
            11'd3  : rom_data = 24'h388928;
            11'd4  : rom_data = 24'h388D28;
            11'd5  : rom_data = 24'h389028;
            11'd6  : rom_data = 24'h389128;
            11'd7  : rom_data = 24'h389328;
            11'd8  : rom_data = 24'h389528;
            11'd9  : rom_data = 24'h389628;
            11'd10 : rom_data = 24'h389828;
            11'd11 : rom_data = 24'h389928;
            11'd12 : rom_data = 24'h389928;
            11'd13 : rom_data = 24'h389A28;
            11'd14 : rom_data = 24'h389B28;
            11'd15 : rom_data = 24'h389C28;
            11'd16 : rom_data = 24'h389D28;
            11'd17 : rom_data = 24'h389D28;
            11'd18 : rom_data = 24'h389E28;
            11'd19 : rom_data = 24'h389F28;
            11'd20 : rom_data = 24'h38A028;
            11'd21 : rom_data = 24'h38A028;
            11'd22 : rom_data = 24'h38A128;
            11'd23 : rom_data = 24'h38A128;
            11'd24 : rom_data = 24'h38A128;
            11'd25 : rom_data = 24'h38A228;
            11'd26 : rom_data = 24'h38A228;
            11'd27 : rom_data = 24'h38A328;
            11'd28 : rom_data = 24'h38A328;
            11'd29 : rom_data = 24'h38A328;
            11'd30 : rom_data = 24'h38A428;
            11'd31 : rom_data = 24'h38A428;
            11'd32 : rom_data = 24'h38A428;
            11'd33 : rom_data = 24'h38A528;
            11'd34 : rom_data = 24'h38A528;
            11'd35 : rom_data = 24'h38A628;
            11'd36 : rom_data = 24'h38A628;
            11'd37 : rom_data = 24'h38A628;
            11'd38 : rom_data = 24'h38A728;
            11'd39 : rom_data = 24'h38A728;
            11'd40 : rom_data = 24'h38A828;
            11'd41 : rom_data = 24'h37A828;
            11'd42 : rom_data = 24'h37A829;
            11'd43 : rom_data = 24'h37A829;
            11'd44 : rom_data = 24'h37A929;
            11'd45 : rom_data = 24'h37A929;
            11'd46 : rom_data = 24'h37A929;
            11'd47 : rom_data = 24'h37A929;
            11'd48 : rom_data = 24'h37A929;
            11'd49 : rom_data = 24'h37A929;
            11'd50 : rom_data = 24'h37AA29;
            11'd51 : rom_data = 24'h37AA29;
            11'd52 : rom_data = 24'h37AA29;
            11'd53 : rom_data = 24'h37AA29;
            11'd54 : rom_data = 24'h37AA29;
            11'd55 : rom_data = 24'h37AB29;
            11'd56 : rom_data = 24'h37AB29;
            11'd57 : rom_data = 24'h37AB29;
            11'd58 : rom_data = 24'h37AB29;
            11'd59 : rom_data = 24'h37AB29;
            11'd60 : rom_data = 24'h37AC29;
            11'd61 : rom_data = 24'h37AC29;
            11'd62 : rom_data = 24'h37AC29;
            11'd63 : rom_data = 24'h37AC29;
            11'd64 : rom_data = 24'h37AC29;
            11'd65 : rom_data = 24'h37AC29;
            11'd66 : rom_data = 24'h37AD29;
            11'd67 : rom_data = 24'h37AD29;
            11'd68 : rom_data = 24'h37AD29;
            11'd69 : rom_data = 24'h37AD29;
            11'd70 : rom_data = 24'h37AD29;
            11'd71 : rom_data = 24'h37AE29;
            11'd72 : rom_data = 24'h36AE29;
            11'd73 : rom_data = 24'h36AE29;
            11'd74 : rom_data = 24'h36AE29;
            11'd75 : rom_data = 24'h36AE29;
            11'd76 : rom_data = 24'h36AE29;
            11'd77 : rom_data = 24'h36AF29;
            11'd78 : rom_data = 24'h36AF29;
            11'd79 : rom_data = 24'h36AF29;
            11'd80 : rom_data = 24'h36AF29;
            11'd81 : rom_data = 24'h36AF29;
            11'd82 : rom_data = 24'h36AF29;
            11'd83 : rom_data = 24'h36B029;
            11'd84 : rom_data = 24'h36B029;
            11'd85 : rom_data = 24'h36B029;
            11'd86 : rom_data = 24'h36B029;
            11'd87 : rom_data = 24'h36B029;
            11'd88 : rom_data = 24'h36B029;
            11'd89 : rom_data = 24'h36B029;
            11'd90 : rom_data = 24'h36B029;
            11'd91 : rom_data = 24'h36B029;
            11'd92 : rom_data = 24'h36B129;
            11'd93 : rom_data = 24'h35B129;
            11'd94 : rom_data = 24'h35B129;
            11'd95 : rom_data = 24'h35B129;
            11'd96 : rom_data = 24'h35B129;
            11'd97 : rom_data = 24'h35B129;
            11'd98 : rom_data = 24'h35B129;
            11'd99 : rom_data = 24'h35B129;
            11'd100: rom_data = 24'h35B129;
            11'd101: rom_data = 24'h35B129;
            11'd102: rom_data = 24'h35B129;
            11'd103: rom_data = 24'h35B129;
            11'd104: rom_data = 24'h35B229;
            11'd105: rom_data = 24'h35B229;
            11'd106: rom_data = 24'h35B229;
            11'd107: rom_data = 24'h35B229;
            11'd108: rom_data = 24'h35B229;
            11'd109: rom_data = 24'h35B229;
            11'd110: rom_data = 24'h34B229;
            11'd111: rom_data = 24'h34B229;
            11'd112: rom_data = 24'h34B229;
            11'd113: rom_data = 24'h34B229;
            11'd114: rom_data = 24'h34B229;
            11'd115: rom_data = 24'h34B229;
            11'd116: rom_data = 24'h34B229;
            11'd117: rom_data = 24'h34B329;
            11'd118: rom_data = 24'h34B319;
            11'd119: rom_data = 24'h34B319;
            11'd120: rom_data = 24'h34B319;
            11'd121: rom_data = 24'h34B319;
            11'd122: rom_data = 24'h34B319;
            11'd123: rom_data = 24'h34B319;
            11'd124: rom_data = 24'h34B319;
            11'd125: rom_data = 24'h34B319;
            11'd126: rom_data = 24'h33B319;
            11'd127: rom_data = 24'h33B319;
            11'd128: rom_data = 24'h33B319;
            11'd129: rom_data = 24'h33B319;
            11'd130: rom_data = 24'h33B319;
            11'd131: rom_data = 24'h33B419;
            11'd132: rom_data = 24'h33B419;
            11'd133: rom_data = 24'h33B419;
            11'd134: rom_data = 24'h33B419;
            11'd135: rom_data = 24'h33B419;
            11'd136: rom_data = 24'h33B419;
            11'd137: rom_data = 24'h33B419;
            11'd138: rom_data = 24'h33B419;
            11'd139: rom_data = 24'h33B41A;
            11'd140: rom_data = 24'h32B41A;
            11'd141: rom_data = 24'h32B41A;
            11'd142: rom_data = 24'h32B41A;
            11'd143: rom_data = 24'h32B41A;
            11'd144: rom_data = 24'h32B41A;
            11'd145: rom_data = 24'h32B41A;
            11'd146: rom_data = 24'h32B41A;
            11'd147: rom_data = 24'h32B51A;
            11'd148: rom_data = 24'h32B51A;
            11'd149: rom_data = 24'h32B51A;
            11'd150: rom_data = 24'h32B51A;
            11'd151: rom_data = 24'h32B51A;
            11'd152: rom_data = 24'h32B51A;
            11'd153: rom_data = 24'h31B51A;
            11'd154: rom_data = 24'h31B51A;
            11'd155: rom_data = 24'h31B51A;
            11'd156: rom_data = 24'h31B51A;
            11'd157: rom_data = 24'h31B51A;
            11'd158: rom_data = 24'h31B51A;
            11'd159: rom_data = 24'h31B51A;
            11'd160: rom_data = 24'h31B51A;
            11'd161: rom_data = 24'h31B51A;
            11'd162: rom_data = 24'h31B51A;
            11'd163: rom_data = 24'h31B51A;
            11'd164: rom_data = 24'h31B61A;
            11'd165: rom_data = 24'h30B61A;
            11'd166: rom_data = 24'h30B61A;
            11'd167: rom_data = 24'h30B61A;
            11'd168: rom_data = 24'h30B61A;
            11'd169: rom_data = 24'h30B61A;
            11'd170: rom_data = 24'h30B61A;
            11'd171: rom_data = 24'h30B61A;
            11'd172: rom_data = 24'h30B61A;
            11'd173: rom_data = 24'h30B61A;
            11'd174: rom_data = 24'h2FB61A;
            11'd175: rom_data = 24'h2FB61A;
            11'd176: rom_data = 24'h2FB61A;
            11'd177: rom_data = 24'h2FB61A;
            11'd178: rom_data = 24'h2FB61A;
            11'd179: rom_data = 24'h2FB61A;
            11'd180: rom_data = 24'h2EB61A;
            11'd181: rom_data = 24'h2EB61A;
            11'd182: rom_data = 24'h2EB61A;
            11'd183: rom_data = 24'h2EB61A;
            11'd184: rom_data = 24'h2EB61A;
            11'd185: rom_data = 24'h2EB71A;
            11'd186: rom_data = 24'h2DB71A;
            11'd187: rom_data = 24'h2DB71A;
            11'd188: rom_data = 24'h2DB71A;
            11'd189: rom_data = 24'h2DB71A;
            11'd190: rom_data = 24'h2DB71A;
            11'd191: rom_data = 24'h2CB71A;
            11'd192: rom_data = 24'h2CB71A;
            11'd193: rom_data = 24'h2CB71A;
            11'd194: rom_data = 24'h2CB71A;
            11'd195: rom_data = 24'h2CB71A;
            11'd196: rom_data = 24'h2CB71A;
            11'd197: rom_data = 24'h2BB71A;
            11'd198: rom_data = 24'h2BB71A;
            11'd199: rom_data = 24'h2BB71A;
            11'd200: rom_data = 24'h2BB71A;
            11'd201: rom_data = 24'h2BB71A;
            11'd202: rom_data = 24'h2AB71A;
            11'd203: rom_data = 24'h2AB71A;
            11'd204: rom_data = 24'h2AB71A;
            11'd205: rom_data = 24'h2AB71A;
            11'd206: rom_data = 24'h2AB71A;
            11'd207: rom_data = 24'h29B71A;
            11'd208: rom_data = 24'h29B71A;
            11'd209: rom_data = 24'h29B71A;
            11'd210: rom_data = 24'h29B71A; // <-- RESTORED 210
            11'd211: rom_data = 24'h29B71A;
            11'd212: rom_data = 24'h29B71A;
            11'd213: rom_data = 24'h28B71A;
            11'd214: rom_data = 24'h28B71A;
            11'd215: rom_data = 24'h28B70A;
            11'd216: rom_data = 24'h28B80A;
            11'd217: rom_data = 24'h27B80A;
            11'd218: rom_data = 24'h27B80A;
            11'd219: rom_data = 24'h26B80A;
            11'd220: rom_data = 24'h26B80A;
            11'd221: rom_data = 24'h26B80A;
            11'd222: rom_data = 24'h25B80A;
            11'd223: rom_data = 24'h25B80A;
            11'd224: rom_data = 24'h24B80A;
            11'd225: rom_data = 24'h24B80A;
            11'd226: rom_data = 24'h24B80A;
            11'd227: rom_data = 24'h23B80A;
            11'd228: rom_data = 24'h23B80A;
            11'd229: rom_data = 24'h23B80A;
            11'd230: rom_data = 24'h22B80A;
            11'd231: rom_data = 24'h22B80A;
            11'd232: rom_data = 24'h21B80A;
            11'd233: rom_data = 24'h21B80A;
            11'd234: rom_data = 24'h21B80A;
            11'd235: rom_data = 24'h20B80A;
            11'd236: rom_data = 24'h20B80A;
            11'd237: rom_data = 24'h1FB80A;
            11'd238: rom_data = 24'h1EB80A;
            11'd239: rom_data = 24'h1DB80A;
            11'd240: rom_data = 24'h1DB80A;
            11'd241: rom_data = 24'h1CB80A;
            11'd242: rom_data = 24'h1BB80A;
            11'd243: rom_data = 24'h1AB80A;
            11'd244: rom_data = 24'h19B80A;
            11'd245: rom_data = 24'h19B80A;
            11'd246: rom_data = 24'h18B80A;
            11'd247: rom_data = 24'h16B80A;
            11'd248: rom_data = 24'h15B80A;
            11'd249: rom_data = 24'h13B80A;
            11'd250: rom_data = 24'h11B80A;
            11'd251: rom_data = 24'h11B80A;
            11'd252: rom_data = 24'h0DB80A;
            11'd253: rom_data = 24'h09B80A;
            11'd254: rom_data = 24'h05B80A;
            11'd255: rom_data = 24'h00B80A;
            11'd256: rom_data = 24'h00B80A;
            11'd257: rom_data = 24'h80B88A;
            11'd258: rom_data = 24'h85B88A;
            11'd259: rom_data = 24'h89B88A;
            11'd260: rom_data = 24'h8DB88A;
            11'd261: rom_data = 24'h90B88A;
            11'd262: rom_data = 24'h91B88A;
            11'd263: rom_data = 24'h93B88A;
            11'd264: rom_data = 24'h95B88A;
            11'd265: rom_data = 24'h96B88A;
            11'd266: rom_data = 24'h98B88A;
            11'd267: rom_data = 24'h99B88A;
            11'd268: rom_data = 24'h99B88A;
            11'd269: rom_data = 24'h9AB88A;
            11'd270: rom_data = 24'h9BB88A;
            11'd271: rom_data = 24'h9CB88A;
            11'd272: rom_data = 24'h9DB88A;
            11'd273: rom_data = 24'h9DB88A;
            11'd274: rom_data = 24'h9EB88A;
            11'd275: rom_data = 24'h9FB88A;
            11'd276: rom_data = 24'hA0B88A;
            11'd277: rom_data = 24'hA0B88A;
            11'd278: rom_data = 24'hA1B88A;
            11'd279: rom_data = 24'hA1B88A;
            11'd280: rom_data = 24'hA1B88A;
            11'd281: rom_data = 24'hA2B88A;
            11'd282: rom_data = 24'hA2B88A;
            11'd283: rom_data = 24'hA3B88A;
            11'd284: rom_data = 24'hA3B88A;
            11'd285: rom_data = 24'hA3B88A;
            11'd286: rom_data = 24'hA4B88A;
            11'd287: rom_data = 24'hA4B88A;
            11'd288: rom_data = 24'hA4B88A;
            11'd289: rom_data = 24'hA5B88A;
            11'd290: rom_data = 24'hA5B88A;
            11'd291: rom_data = 24'hA6B88A;
            11'd292: rom_data = 24'hA6B88A;
            11'd293: rom_data = 24'hA6B88A;
            11'd294: rom_data = 24'hA7B88A;
            11'd295: rom_data = 24'hA7B88A;
            11'd296: rom_data = 24'hA8B88A;
            11'd297: rom_data = 24'hA8B78A;
            11'd298: rom_data = 24'hA8B79A;
            11'd299: rom_data = 24'hA8B79A;
            11'd300: rom_data = 24'hA9B79A;
            11'd301: rom_data = 24'hA9B79A;
            11'd302: rom_data = 24'hA9B79A;
            11'd303: rom_data = 24'hA9B79A;
            11'd304: rom_data = 24'hA9B79A;
            11'd305: rom_data = 24'hA9B79A;
            11'd306: rom_data = 24'hAAB79A;
            11'd307: rom_data = 24'hAAB79A;
            11'd308: rom_data = 24'hAAB79A;
            11'd309: rom_data = 24'hAAB79A;
            11'd310: rom_data = 24'hAAB79A; // <-- RESTORED 310
            11'd311: rom_data = 24'hABB79A;
            11'd312: rom_data = 24'hABB79A;
            11'd313: rom_data = 24'hABB79A;
            11'd314: rom_data = 24'hABB79A;
            11'd315: rom_data = 24'hABB79A;
            11'd316: rom_data = 24'hACB79A;
            11'd317: rom_data = 24'hACB79A;
            11'd318: rom_data = 24'hACB79A;
            11'd319: rom_data = 24'hACB79A;
            11'd320: rom_data = 24'hACB79A;
            11'd321: rom_data = 24'hACB79A;
            11'd322: rom_data = 24'hADB79A;
            11'd323: rom_data = 24'hADB79A;
            11'd324: rom_data = 24'hADB79A;
            11'd325: rom_data = 24'hADB79A;
            11'd326: rom_data = 24'hADB79A;
            11'd327: rom_data = 24'hAEB79A;
            11'd328: rom_data = 24'hAEB69A;
            11'd329: rom_data = 24'hAEB69A;
            11'd330: rom_data = 24'hAEB69A;
            11'd331: rom_data = 24'hAEB69A;
            11'd332: rom_data = 24'hAEB69A;
            11'd333: rom_data = 24'hAFB69A;
            11'd334: rom_data = 24'hAFB69A;
            11'd335: rom_data = 24'hAFB69A;
            11'd336: rom_data = 24'hAFB69A;
            11'd337: rom_data = 24'hAFB69A;
            11'd338: rom_data = 24'hAFB69A;
            11'd339: rom_data = 24'hB0B69A;
            11'd340: rom_data = 24'hB0B69A;
            11'd341: rom_data = 24'hB0B69A;
            11'd342: rom_data = 24'hB0B69A;
            11'd343: rom_data = 24'hB0B69A;
            11'd344: rom_data = 24'hB0B69A;
            11'd345: rom_data = 24'hB0B69A;
            11'd346: rom_data = 24'hB0B69A;
            11'd347: rom_data = 24'hB0B69A;
            11'd348: rom_data = 24'hB1B69A;
            11'd349: rom_data = 24'hB1B59A;
            11'd350: rom_data = 24'hB1B59A;
            11'd351: rom_data = 24'hB1B59A;
            11'd352: rom_data = 24'hB1B59A;
            11'd353: rom_data = 24'hB1B59A;
            11'd354: rom_data = 24'hB1B59A;
            11'd355: rom_data = 24'hB1B59A;
            11'd356: rom_data = 24'hB1B59A;
            11'd357: rom_data = 24'hB1B59A;
            11'd358: rom_data = 24'hB1B59A;
            11'd359: rom_data = 24'hB1B59A;
            11'd360: rom_data = 24'hB2B59A;
            11'd361: rom_data = 24'hB2B59A;
            11'd362: rom_data = 24'hB2B59A;
            11'd363: rom_data = 24'hB2B59A;
            11'd364: rom_data = 24'hB2B59A;
            11'd365: rom_data = 24'hB2B59A;
            11'd366: rom_data = 24'hB2B49A;
            11'd367: rom_data = 24'hB2B49A;
            11'd368: rom_data = 24'hB2B49A;
            11'd369: rom_data = 24'hB2B49A;
            11'd370: rom_data = 24'hB2B49A;
            11'd371: rom_data = 24'hB2B49A;
            11'd372: rom_data = 24'hB2B49A;
            11'd373: rom_data = 24'hB3B49A;
            11'd374: rom_data = 24'hB3B499;
            11'd375: rom_data = 24'hB3B499;
            11'd376: rom_data = 24'hB3B499;
            11'd377: rom_data = 24'hB3B499;
            11'd378: rom_data = 24'hB3B499;
            11'd379: rom_data = 24'hB3B499;
            11'd380: rom_data = 24'hB3B499;
            11'd381: rom_data = 24'hB3B499;
            11'd382: rom_data = 24'hB3B399;
            11'd383: rom_data = 24'hB3B399;
            11'd384: rom_data = 24'hB3B399;
            11'd385: rom_data = 24'hB3B399;
            11'd386: rom_data = 24'hB3B399;
            11'd387: rom_data = 24'hB4B399;
            11'd388: rom_data = 24'hB4B399;
            11'd389: rom_data = 24'hB4B399;
            11'd390: rom_data = 24'hB4B399;
            11'd391: rom_data = 24'hB4B399;
            11'd392: rom_data = 24'hB4B399;
            11'd393: rom_data = 24'hB4B399;
            11'd394: rom_data = 24'hB4B399;
            11'd395: rom_data = 24'hB4B3A9;
            11'd396: rom_data = 24'hB4B2A9;
            11'd397: rom_data = 24'hB4B2A9;
            11'd398: rom_data = 24'hB4B2A9;
            11'd399: rom_data = 24'hB4B2A9;
            11'd400: rom_data = 24'hB4B2A9;
            11'd401: rom_data = 24'hB4B2A9;
            11'd402: rom_data = 24'hB4B2A9;
            11'd403: rom_data = 24'hB5B2A9;
            11'd404: rom_data = 24'hB5B2A9;
            11'd405: rom_data = 24'hB5B2A9;
            11'd406: rom_data = 24'hB5B2A9;
            11'd407: rom_data = 24'hB5B2A9;
            11'd408: rom_data = 24'hB5B2A9;
            11'd409: rom_data = 24'hB5B1A9;
            11'd410: rom_data = 24'hB5B1A9; // <-- RESTORED 410
            11'd411: rom_data = 24'hB5B1A9;
            11'd412: rom_data = 24'hB5B1A9;
            11'd413: rom_data = 24'hB5B1A9;
            11'd414: rom_data = 24'hB5B1A9;
            11'd415: rom_data = 24'hB5B1A9;
            11'd416: rom_data = 24'hB5B1A9;
            11'd417: rom_data = 24'hB5B1A9;
            11'd418: rom_data = 24'hB5B1A9;
            11'd419: rom_data = 24'hB5B1A9;
            11'd420: rom_data = 24'hB6B1A9;
            11'd421: rom_data = 24'hB6B0A9;
            11'd422: rom_data = 24'hB6B0A9;
            11'd423: rom_data = 24'hB6B0A9;
            11'd424: rom_data = 24'hB6B0A9;
            11'd425: rom_data = 24'hB6B0A9;
            11'd426: rom_data = 24'hB6B0A9;
            11'd427: rom_data = 24'hB6B0A9;
            11'd428: rom_data = 24'hB6B0A9;
            11'd429: rom_data = 24'hB6B0A9;
            11'd430: rom_data = 24'hB6AFA9;
            11'd431: rom_data = 24'hB6AFA9;
            11'd432: rom_data = 24'hB6AFA9;
            11'd433: rom_data = 24'hB6AFA9;
            11'd434: rom_data = 24'hB6AFA9;
            11'd435: rom_data = 24'hB6AFA9;
            11'd436: rom_data = 24'hB6AEA9;
            11'd437: rom_data = 24'hB6AEA9;
            11'd438: rom_data = 24'hB6AEA9;
            11'd439: rom_data = 24'hB6AEA9;
            11'd440: rom_data = 24'hB6AEA9;
            11'd441: rom_data = 24'hB7AEA9;
            11'd442: rom_data = 24'hB7ADA9;
            11'd443: rom_data = 24'hB7ADA9;
            11'd444: rom_data = 24'hB7ADA9;
            11'd445: rom_data = 24'hB7ADA9;
            11'd446: rom_data = 24'hB7ADA9;
            11'd447: rom_data = 24'hB7ACA9;
            11'd448: rom_data = 24'hB7ACA9;
            11'd449: rom_data = 24'hB7ACA9;
            11'd450: rom_data = 24'hB7ACA9;
            11'd451: rom_data = 24'hB7ACA9;
            11'd452: rom_data = 24'hB7ACA9;
            11'd453: rom_data = 24'hB7ABA9;
            11'd454: rom_data = 24'hB7ABA9;
            11'd455: rom_data = 24'hB7ABA9;
            11'd456: rom_data = 24'hB7ABA9;
            11'd457: rom_data = 24'hB7ABA9;
            11'd458: rom_data = 24'hB7AAA9;
            11'd459: rom_data = 24'hB7AAA9;
            11'd460: rom_data = 24'hB7AAA9;
            11'd461: rom_data = 24'hB7AAA9;
            11'd462: rom_data = 24'hB7AAA9;
            11'd463: rom_data = 24'hB7A9A9;
            11'd464: rom_data = 24'hB7A9A9;
            11'd465: rom_data = 24'hB7A9A9;
            11'd466: rom_data = 24'hB7A9A9;
            11'd467: rom_data = 24'hB7A9A9;
            11'd468: rom_data = 24'hB7A9A9;
            11'd469: rom_data = 24'hB7A8A9;
            11'd470: rom_data = 24'hB7A8A9;
            11'd471: rom_data = 24'hB7A8A8;
            11'd472: rom_data = 24'hB8A8A8;
            11'd473: rom_data = 24'hB8A7A8;
            11'd474: rom_data = 24'hB8A7A8;
            11'd475: rom_data = 24'hB8A6A8;
            11'd476: rom_data = 24'hB8A6A8;
            11'd477: rom_data = 24'hB8A6A8;
            11'd478: rom_data = 24'hB8A5A8;
            11'd479: rom_data = 24'hB8A5A8;
            11'd480: rom_data = 24'hB8A4A8;
            11'd481: rom_data = 24'hB8A4A8;
            11'd482: rom_data = 24'hB8A4A8;
            11'd483: rom_data = 24'hB8A3A8;
            11'd484: rom_data = 24'hB8A3A8;
            11'd485: rom_data = 24'hB8A3A8;
            11'd486: rom_data = 24'hB8A2A8;
            11'd487: rom_data = 24'hB8A2A8;
            11'd488: rom_data = 24'hB8A1A8;
            11'd489: rom_data = 24'hB8A1A8;
            11'd490: rom_data = 24'hB8A1A8;
            11'd491: rom_data = 24'hB8A0A8;
            11'd492: rom_data = 24'hB8A0A8;
            11'd493: rom_data = 24'hB89FA8;
            11'd494: rom_data = 24'hB89EA8;
            11'd495: rom_data = 24'hB89DA8;
            11'd496: rom_data = 24'hB89DA8;
            11'd497: rom_data = 24'hB89CA8;
            11'd498: rom_data = 24'hB89BA8;
            11'd499: rom_data = 24'hB89AA8;
            11'd500: rom_data = 24'hB899A8;
            11'd501: rom_data = 24'hB899A8;
            11'd502: rom_data = 24'hB898A8;
            11'd503: rom_data = 24'hB896A8;
            11'd504: rom_data = 24'hB895A8;
            11'd505: rom_data = 24'hB893A8;
            11'd506: rom_data = 24'hB891A8;
            11'd507: rom_data = 24'hB890A8;
            11'd508: rom_data = 24'hB88DA8;
            11'd509: rom_data = 24'hB889A8;
            11'd510: rom_data = 24'hB885A8;
            11'd511: rom_data = 24'hB880A8;
            default: rom_data = 24'h380020; // fallback: W=1+0j
        endcase
    end

    // -------------------------------------------------------------------------
    // 4. Registered output — 1-cycle latency, with conj and midpoint handling
    // -------------------------------------------------------------------------
    always @(posedge clk) begin
        if (is_mid) begin
            // W_N^(N/2) = -1+0j
            twiddle_out <= PRECISION ? 16'hB800    // FP8: -1.0+0j
                                     : 16'h00A0;   // FP4: -1.0+0j (zero-padded upper)
        end else if (PRECISION) begin
            // FP8 output: [15:8]=real, [7:0]=imag
            twiddle_out[15:8] <= rom_data[23:16];
            twiddle_out[7:0]  <= use_conj ? {~rom_data[15], rom_data[14:8]}
                                          :   rom_data[15:8];
        end else begin
            // FP4 output: upper 8 bits zero, [7:4]=real, [3:0]=imag
            twiddle_out[15:8] <= 8'h00;
            twiddle_out[7:4]  <= rom_data[7:4];
            twiddle_out[3:0]  <= use_conj ? {~rom_data[3], rom_data[2:0]}
                                          :   rom_data[3:0];
        end
    end

endmodule